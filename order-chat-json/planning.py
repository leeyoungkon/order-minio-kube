"""모델 계획의 내부 일관성 보정과 간결한 교정 피드백."""
from __future__ import annotations

import json
import re
from query_engine import PipelinePlan,QueryStep,JoinStep,CompareStep,CalculateStep,WindowStep

ORDER_COLUMNS = {'order_id','customer_id','product_id','product_name',
                 'quantity','amount','order_time'}


def compact_schema(value,names=False):
    """검증 규칙은 보존하고 설명용 title/default만 제거합니다."""
    if isinstance(value,dict):
        return {('anyOf' if key=='oneOf' else key):compact_schema(item,key in {'properties','$defs'})
                for key,item in value.items() if names or key not in {'title','default','discriminator'}}
    if isinstance(value,list):
        return [compact_schema(item) for item in value]
    return value


def normalize_model_plan(raw):
    """query에 중복된 비교 지표만 삭제합니다. 필요한 계산은 추측하지 않습니다."""
    value=json.loads(raw)
    changes=[]
    if not isinstance(value,dict) or not isinstance(value.get('steps'),list):
        return raw,changes
    for step in value['steps']:
        if not isinstance(step,dict) or step.get('operation','query')!='query':continue
        metrics=step.get('metrics')
        aliases={a.get('alias') for a in step.get('aggregates',[]) if isinstance(a,dict)}
        if (isinstance(metrics,list) and metrics and all(isinstance(m,str) for m in metrics)
                and set(metrics)<=aliases and not step.get('right') and not step.get('on')):
            step.pop('metrics')
            changes.append(dict(step=step.get('id'),field='metrics',before=metrics,after=[],
                                reason='query의 aggregates에 이미 명시된 중복 비교용 metrics를 제거했습니다. 집계는 그대로 실행합니다.'))
    return json.dumps(value,ensure_ascii=False,separators=(',',':')),changes


def choose_examples(question,examples,limit=2):
    """질문을 분기하지 않고, 가까운 연산 예시만 모델에 제공합니다."""
    def grams(text):
        text=re.sub(r'\s+','',text.lower())
        return {text[i:i+2] for i in range(len(text)-1)}
    target=grams(question)
    def score(example):
        other=grams(example['question'])
        return len(target & other)/max(1,len(target | other))
    return sorted(examples,key=score,reverse=True)[:limit]


def repair_compare_grain(plan):
    """compare의 명시적 키를, 그 비교만을 위한 원본 집계 단계에 보존합니다.

    필터/기간/지표/상위 제한을 바꾸지 않습니다. 결과 재사용, projection,
    원본에 없는 키, 다른 집계 단위와 얽힌 단계는 모델 교정에 맡깁니다.
    """
    payload=plan.model_dump(mode='json',exclude_unset=True)
    steps={step.id:step for step in plan.steps}
    changes=[]
    for comparison in plan.steps:
        if comparison.operation!='compare' or not comparison.keys:
            continue
        for name in dict.fromkeys([comparison.source,comparison.right]):
            source=steps[name]
            if (source.operation!='query' or source.source!='orders' or
                    not source.aggregates or source.select or source.limit or
                    source.time_bucket or source.group_by or plan.output==name):
                continue
            if not set(comparison.keys)<=ORDER_COLUMNS:
                continue
            if set(comparison.keys)&{a.alias for a in source.aggregates}:
                continue
            users=set()
            for step in plan.steps:
                if name in (step.source,getattr(step,'right',None)):users.add(step.id)
                for predicate in getattr(step,'filters',[]):
                    if predicate.ref and predicate.ref.step==name:users.add(step.id)
                for calculation in getattr(step,'calculations',[]):
                    for operand in (calculation.left,calculation.right):
                        if operand.ref and operand.ref.step==name:users.add(step.id)
            if users!={comparison.id}:
                continue
            for step in payload['steps']:
                if step['id']==name:step['group_by']=list(comparison.keys)
            changes.append(dict(step=name,compare_step=comparison.id,
                                field='group_by',before=[],after=list(comparison.keys),
                                reason='고객·제품별 비교에 필요한 비교 키를 원본 집계 결과에 보존했습니다.'))
    return PipelinePlan.model_validate(payload),changes


def repair_feedback(raw,error):
    try:
        parsed=PipelinePlan.model_validate_json(raw)
        previous=parsed.model_dump(mode='json',exclude_unset=True)
    except (ValueError,TypeError):
        try:previous=json.loads(raw)
        except (ValueError,TypeError):previous=str(raw)[:12000]
    diagnostics=getattr(error,'diagnostics',None)
    if hasattr(error,'errors'):
        types={'query':QueryStep,'join':JoinStep,'compare':CompareStep,
               'calculate':CalculateStep,'window':WindowStep}
        errors=[]
        for item in error.errors(include_url=False,include_context=False,include_input=False):
            errors.append(item)
        diagnostics=dict(validation_errors=errors,
                         allowed_fields={name:list(model.model_fields) for name,model in types.items()},
                         hints=['query에는 aggregates로 집계 지표를 지정합니다. metrics는 compare 전용입니다.',
                                'query의 right/on은 join 전용입니다. 연결이 필요하면 join을 만들고 다음 query로 정렬·필터하세요.',
                                'calculate/window 단계의 결과를 정렬·필터하려면 별도 query 단계를 만드세요.'])
    return json.dumps(dict(previous_plan=previous,error=str(error),
                           diagnostics=diagnostics,
                           instruction='원래 질문의 기간·대상·지표를 유지하세요. 실제 존재하는 칼럼만 사용하고 '
                           '뒤에서 필요한 고객·제품 키는 앞 집계 group_by에 보존하세요. '
                           '에러를 피하려고 고객별 질문을 전체 합계로 바꾸지 마세요. 전체 JSON 계획을 반환하세요.'),
                      ensure_ascii=False,separators=(',',':'))


def validation_summary(raw,error):
    """화면에는 입력값 덤프·내부 링크 대신 실패 단계와 필드를 표시합니다."""
    if not hasattr(error,'errors'):return str(error)[:400]
    try:steps=json.loads(raw).get('steps',[])
    except (ValueError,TypeError,AttributeError):steps=[]
    owners={'metrics':'compare','keys':'compare','right':'join/compare','on':'join','how':'join',
            'calculations':'calculate','partition_by':'window','window_order_by':'window','windows':'window',
            'filters':'query','period':'query','aggregates':'query','group_by':'query',
            'time_bucket':'query','order_by':'query','limit':'query','select':'query'}
    lines=[]
    for item in error.errors(include_url=False,include_context=False,include_input=False)[:3]:
        loc=item['loc'];index=loc[1] if len(loc)>1 and loc[0]=='steps' and isinstance(loc[1],int) else None
        step=steps[index] if index is not None and index<len(steps) and isinstance(steps[index],dict) else {}
        prefix=step.get('id',f'단계 {index+1}' if index is not None else '계획')
        field=str(loc[-1]) if loc else ''
        if item['type']=='extra_forbidden':
            message=f'{field}는 {owners.get(field,"다른 연산")} 전용 필드입니다.'
        elif item['type']=='missing':message=f'필수 필드 {field}가 누락되었습니다.'
        else:message=item['msg'].removeprefix('Value error, ')
        lines.append(prefix+': '+message)
    return ' '.join(lines)[:400]
