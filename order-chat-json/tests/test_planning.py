import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app import create_app,Question,Ollama,metadata_for
from query_engine import PipelinePlan,PlanError,execute
from planning import compact_schema,choose_examples,repair_compare_grain,repair_feedback
from test_engine import NOW,rows,q,agg,EXAMPLES,order
from test_chat import LLM,Store


def bad_comparison():
 return dict(steps=[q('s1',period={'kind':'today'},aggregates=[agg('sum','amount','amount')]),
                    q('s2',period={'kind':'yesterday'},aggregates=[agg('sum','amount','amount')]),
                    dict(id='s3',title='오늘과 어제 전체 비교',operation='compare',source='s1',right='s2',keys=['customer_id'],metrics=['amount']),
                    q('s4',source='s3',filters=[dict(column='delta_amount',operator='gt',values=[0])],order_by=[dict(column='delta_amount')],limit=5)],output='s4')


def test_screenshot_error_reproduced_then_customer_totals_repaired():
 original=PipelinePlan.model_validate(bad_comparison())
 with pytest.raises(PlanError,match='존재하지 않는 칼럼 customer_id') as failed:execute(original,rows(),NOW)
 assert failed.value.diagnostics['available_columns']['s1']=={'amount':'number'}
 repaired,changes=repair_compare_grain(original)
 assert original.steps[0].group_by==[] and len(changes)==2
 evidence=execute(repaired,rows(),NOW)
 assert [r['customer_id'] for r in evidence['rows']]==['C001','C002','C004']
 assert evidence['rows'][0]['delta_amount']=='100'
 assert repaired.steps[0].period==original.steps[0].period


def test_chat_repairs_model_plan_in_single_call_and_shows_changed_grain():
 llm=LLM(bad_comparison())
 with TestClient(create_app(Store(),llm,start_poller=False)) as client:
  response=client.post('/api/chat',json={'question':'오늘 주문액이 어제 전체보다 증가한 고객 상위 5명은?'})
 assert response.status_code==200 and len(llm.calls)==1
 value=response.json()
 assert value['plan']['steps'][0]['group_by']==['customer_id']
 assert len(value['evidence']['plan_repairs'])==2
 assert '모델 계획 보정' in value['evidence']['steps'][0]['notes'][-1]


@pytest.mark.parametrize('change',['shared_total','limit','projection','unknown_key','alias_collision'])
def test_ambiguous_or_semantic_changes_are_not_automatically_repaired(change):
 payload=bad_comparison()
 if change=='shared_total':
  payload['steps'].append(dict(id='s5',title='share',operation='calculate',source='s4',calculations=[dict(alias='share',function='percent',left={'column':'delta_amount'},right={'ref':{'step':'s1','column':'amount'}})]));payload['output']='s5'
 elif change=='limit':payload['steps'][0]['limit']=1
 elif change=='projection':payload['steps'][0]['select']=['amount']
 elif change=='unknown_key':payload['steps'][2]['keys']=['customer_name']
 else:
  payload['steps'][0]['aggregates']=[agg('count','*','customer_id')]
  payload['steps'][2]['metrics']=['customer_id']
 repaired,_=repair_compare_grain(PipelinePlan.model_validate(payload))
 assert repaired.steps[0].group_by==[]


def test_correct_global_and_customer_comparisons_are_unchanged():
 payload=bad_comparison();payload['steps'][2]['keys']=[]
 repaired,changes=repair_compare_grain(PipelinePlan.model_validate(payload))
 assert changes==[] and execute(repaired,rows(),NOW)['rows'][0]['delta_amount']=='120'
 original=PipelinePlan.model_validate(EXAMPLES[3]['plan'])
 repaired,changes=repair_compare_grain(original)
 assert changes==[] and repaired==original


def test_compact_schema_preserves_title_property_and_all_validation_rules():
 schema=compact_schema(PipelinePlan.model_json_schema())
 step=schema['$defs']['Step']
 assert 'title' in step['properties'] and 'title' in step['required']
 assert step['properties']['title']['maxLength']==120
 assert step['additionalProperties'] is False
 assert 'title' not in step and 'default' not in step['properties']['operation']
 assert schema['properties']['steps']['maxItems']==10


def test_correction_contains_source_columns_and_grouping_advice():
 payload=bad_comparison();payload['steps'][0]['select']=['amount']
 plan=PipelinePlan.model_validate(payload)
 with pytest.raises(PlanError) as failed:execute(plan,rows(),NOW)
 feedback=json.loads(repair_feedback(json.dumps(payload),failed.value))
 assert feedback['diagnostics']['available_columns']['s1']=={'amount':'number'}
 assert 'group_by' in feedback['diagnostics']['hints'][0]
 assert feedback['previous_plan']['steps'][0]['select']==['amount']


def test_similar_example_is_retrieved_without_routing_question_to_fixed_plan():
 chosen=choose_examples('오늘 주문액이 어제 전체보다 증가한 고객 상위 5명은?',EXAMPLES)
 assert len(chosen)==2 and chosen[0]==EXAMPLES[3]


def test_compact_prompt_contains_only_two_examples_and_correct_schema():
 import asyncio
 captured=[]
 class Capture(Ollama):
  async def chat(self,prompt,schema=None):captured.append((prompt,schema));return '{}'
 llm=Capture(None,'http://ollama','gemma3:12b')
 question=Question(question=EXAMPLES[3]['question'])
 asyncio.run(llm.plan(question,metadata_for(rows(),NOW,[])))
 prompt,schema=captured[0]
 sample=prompt.split('examples:\n')[1].split('\n현재 질문')[0]
 assert len(json.loads(sample))==2
 assert 'title' in schema['$defs']['Step']['properties']
