"""JSON 조회 계획을 실행하는 제한된 관계 연산기. SQL/Python 문자열을 실행하지 않습니다."""
from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

KST = ZoneInfo('Asia/Seoul')
PRODUCTS = {'P001':'Laptop','P002':'Monitor','P003':'Keyboard','P004':'Mouse','P005':'Server'}
MAX_ROWS = 20000
MAX_BINS = 1000
ID_PATTERN = r'^[a-z][a-z0-9_]{0,39}$'
COL_PATTERN = r'^[a-z][a-z0-9_]{0,119}$'
Scalar = str | int | float | None


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Period(Model):
    kind: Literal['today','yesterday','last5m','last1h','last24h','last7d','last30d','thisweek','thismonth','all','custom'] = 'today'
    start: datetime | None = None
    end: datetime | None = None

    @model_validator(mode='after')
    def valid(self):
        if self.kind == 'custom':
            if self.start is None or self.end is None or kst(self.start) >= kst(self.end):
                raise ValueError('custom 기간에는 시작보다 뒤인 종료 시각이 필요합니다.')
        elif self.start is not None or self.end is not None:
            raise ValueError('start/end는 custom 기간에만 사용합니다.')
        return self


class Ref(Model):
    step: str = Field(pattern=ID_PATTERN)
    column: str = Field(pattern=COL_PATTERN)


class Predicate(Model):
    column: str = Field(pattern=COL_PATTERN)
    operator: Literal['eq','ne','in','not_in','gt','ge','lt','le','contains','is_null','not_null'] = 'in'
    values: list[Scalar] = Field(default_factory=list, max_length=100)
    ref: Ref | None = None

    @model_validator(mode='after')
    def valid(self):
        if self.ref and self.values:
            raise ValueError('필터는 values 또는 ref 중 하나만 사용합니다.')
        if self.operator in {'is_null','not_null'}:
            if self.values or self.ref:
                raise ValueError('null 필터에는 값 또는 참조가 필요 없습니다.')
        elif self.operator not in {'in','not_in'} and not self.ref and len(self.values) != 1:
            raise ValueError('단일값 필터는 values에 값 한 개가 필요합니다.')
        return self


class Aggregate(Model):
    function: Literal['sum','count','avg','min','max','count_distinct']
    column: str = Field(pattern=r'^(\*|[a-z][a-z0-9_]{0,119})$')
    alias: str = Field(pattern=ID_PATTERN)

    @model_validator(mode='after')
    def valid(self):
        if self.alias == 'evidence_id':
            raise ValueError('evidence_id는 근거 표시용 예약 칼럼입니다.')
        if self.column == '*' and self.function != 'count':
            raise ValueError('*는 count에서만 사용합니다.')
        return self


class Sort(Model):
    column: str = Field(pattern=COL_PATTERN)
    direction: Literal['asc','desc'] = 'desc'


class JoinKey(Model):
    left: str = Field(pattern=COL_PATTERN)
    right: str = Field(pattern=COL_PATTERN)


class Operand(Model):
    column: str | None = Field(default=None, pattern=COL_PATTERN)
    value: float | int | None = None
    ref: Ref | None = None

    @model_validator(mode='after')
    def valid(self):
        if sum(v is not None for v in (self.column,self.value,self.ref)) != 1:
            raise ValueError('피연산자는 column/value/ref 중 하나가 필요합니다.')
        return self


class Calculation(Model):
    alias: str = Field(pattern=ID_PATTERN)
    function: Literal['add','subtract','multiply','divide','percent']
    left: Operand
    right: Operand

    @model_validator(mode='after')
    def valid(self):
        if self.alias == 'evidence_id':
            raise ValueError('evidence_id는 근거 표시용 예약 칼럼입니다.')
        return self


class Window(Model):
    function: Literal['lag','running_sum','row_number','rank']
    alias: str = Field(pattern=ID_PATTERN)
    column: str | None = Field(default=None,pattern=COL_PATTERN)
    offset: int = Field(default=1,ge=1,le=500)

    @model_validator(mode='after')
    def valid(self):
        if self.alias == 'evidence_id':raise ValueError('예약 칼럼 이름입니다.')
        if self.function in {'lag','running_sum'} and self.column is None:
            raise ValueError('lag/running_sum에는 column이 필요합니다.')
        if self.function in {'rank','row_number'} and self.column is not None:
            raise ValueError('rank/row_number는 정렬 조건만 사용합니다.')
        return self


class StepBase(Model):
    id: str = Field(pattern=ID_PATTERN)
    title: str = Field(min_length=1, max_length=120)
    source: str = Field(default='orders', pattern=ID_PATTERN)

    @model_validator(mode='after')
    def unique_columns(self):
        for name in ('group_by','select','keys','metrics','partition_by'):
            columns = getattr(self,name,[])
            if len(columns) != len(set(columns)):
                raise ValueError('칼럼을 중복 지정할 수 없습니다.')
            for column in columns:
                Ref(step='s',column=column)
        return self


class QueryStep(StepBase):
    operation: Literal['query']
    period: Period | None = None
    filters: list[Predicate] = Field(default_factory=list, max_length=12)
    group_by: list[str] = Field(default_factory=list, max_length=4)
    aggregates: list[Aggregate] = Field(default_factory=list, max_length=10)
    time_bucket: Literal['15seconds','minute','5minutes','hour','day'] | None = None
    time_field: str = Field(default='order_time', pattern=COL_PATTERN)
    fill_gaps: bool = False
    order_by: list[Sort] = Field(default_factory=list, max_length=4)
    limit: int | None = Field(default=None, ge=1, le=500)
    keep_ties: bool = False
    select: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode='after')
    def valid(self):
        if self.group_by and not self.aggregates:
            raise ValueError('group_by에는 aggregates가 필요합니다.')
        if self.keep_ties and (not self.order_by or self.limit is None):
            raise ValueError('keep_ties에는 order_by와 limit이 필요합니다.')
        if self.fill_gaps and ('time_bucket' not in self.group_by or not self.time_bucket or not self.aggregates):
            raise ValueError('fill_gaps는 time_bucket을 포함한 시간별 집계에서 사용합니다.')
        return self


class JoinStep(StepBase):
    operation: Literal['join']
    right: str = Field(pattern=ID_PATTERN)
    on: list[JoinKey] = Field(min_length=1,max_length=4)
    how: Literal['inner','left'] = 'inner'


class CompareStep(StepBase):
    operation: Literal['compare']
    right: str = Field(pattern=ID_PATTERN)
    keys: list[str] = Field(default_factory=list,max_length=4)
    metrics: list[str] = Field(min_length=1,max_length=10)


class CalculateStep(StepBase):
    operation: Literal['calculate']
    calculations: list[Calculation] = Field(min_length=1,max_length=10)


class WindowStep(StepBase):
    operation: Literal['window']
    partition_by: list[str] = Field(default_factory=list,max_length=4)
    window_order_by: list[Sort] = Field(min_length=1,max_length=4)
    windows: list[Window] = Field(min_length=1,max_length=10)


Step = Annotated[QueryStep | JoinStep | CompareStep | CalculateStep | WindowStep,
                 Field(discriminator='operation')]


class PipelinePlan(Model):
    status: Literal['ready','clarify','unsupported'] = 'ready'
    reason: str = Field(default='', max_length=500)
    steps: list[Step] = Field(default_factory=list, max_length=10)
    output: str | None = Field(default=None, pattern=ID_PATTERN)

    @model_validator(mode='before')
    @classmethod
    def legacy_defaults(cls,value):
        # 2.0/2.0.1 API가 내보낸 공통 Step의 빈 기본값만 호환합니다.
        # 실제 연결·비교·계산 조건은 제거하지 않습니다.
        if not isinstance(value,dict) or not isinstance(value.get('steps'),list):
            return value
        types={'query':QueryStep,'join':JoinStep,'compare':CompareStep,
               'calculate':CalculateStep,'window':WindowStep}
        defaults={'period':None,'filters':[],'group_by':[],'aggregates':[],
                  'time_bucket':None,'time_field':'order_time','fill_gaps':False,
                  'order_by':[],'limit':None,'keep_ties':False,'select':[],
                  'right':None,'on':[],'how':'inner','keys':[],'metrics':[],
                  'calculations':[],'partition_by':[],'window_order_by':[],'windows':[]}
        steps=[]
        for raw in value['steps']:
            if not isinstance(raw,dict):
                steps.append(raw);continue
            step=dict(raw)
            operation=step.setdefault('operation','query')
            model=types.get(operation)
            if model:
                step={key:item for key,item in step.items() if not
                      (key not in model.model_fields and key in defaults and item==defaults[key])}
            steps.append(step)
        return dict(value,steps=steps)

    @model_validator(mode='after')
    def valid(self):
        if self.status != 'ready':
            if not self.reason or self.steps or self.output:
                raise ValueError('clarify/unsupported에는 reason을 쓰고 steps는 비워야 합니다.')
            return self
        if not self.steps or self.output is None:
            raise ValueError('ready에는 steps와 output이 필요합니다.')
        seen = {'orders'}
        for step in self.steps:
            if step.id in seen:
                raise ValueError('단계 ID는 orders와 다르고 중복되지 않아야 합니다.')
            right = getattr(step,'right',None)
            sources = [step.source] + ([right] if right else [])
            if step.operation != 'query' and 'orders' in sources:
                raise ValueError('join/compare/calculate/window는 앞 query 결과를 source/right로 사용하세요.')
            for predicate in getattr(step,'filters',[]):
                if predicate.ref:
                    if predicate.ref.step == 'orders':
                        raise ValueError('ref는 원본 orders가 아니라 앞 단계 결과를 참조해야 합니다.')
                    sources.append(predicate.ref.step)
            for calculation in getattr(step,'calculations',[]):
                for operand in (calculation.left,calculation.right):
                    if operand.ref:
                        if operand.ref.step == 'orders':
                            raise ValueError('ref는 원본 orders가 아니라 앞 단계 결과를 참조해야 합니다.')
                        sources.append(operand.ref.step)
            if any(source not in seen for source in sources):
                raise ValueError(f'{step.id}: source/ref는 앞 단계만 참조할 수 있습니다.')
            seen.add(step.id)
        if self.output not in seen or self.output == 'orders':
            raise ValueError('output은 실행 단계 ID여야 합니다.')
        return self


class PlanError(ValueError):
    def __init__(self, message, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics


@dataclass
class Table:
    rows: list[dict]
    columns: dict[str,str]
    notes: list[str]


def kst(value):
    return (value.replace(tzinfo=KST) if value.tzinfo is None else value).astimezone(KST)


def number(value):
    if value is None:
        return None
    if isinstance(value,bool) or not isinstance(value,(int,float,Decimal)):
        raise PlanError(f'숫자 칼럼이 필요합니다: {value!r}')
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise PlanError('숫자는 유한한 값이어야 합니다.')
    return parsed


def public(value):
    if isinstance(value,Decimal):
        return format(value,'f')
    if isinstance(value,datetime):
        return kst(value).isoformat()
    if isinstance(value,dict):
        return {key:public(item) for key,item in value.items()}
    if isinstance(value,(list,tuple)):
        return [public(item) for item in value]
    return value


class Executor:
    def __init__(self,orders,now,seconds=30):
        self.now = kst(now)
        self.deadline = time.monotonic()+seconds
        self.tables = {'orders':Table([
            dict(order_id=r.order_id,customer_id=r.customer_id,product_id=r.product_id,
                 product_name=PRODUCTS.get(r.product_id,r.product_id),quantity=r.quantity,
                 amount=r.amount,order_time=kst(r.ordered_at)) for r in orders
        ],dict(order_id='string',customer_id='string',product_id='string',product_name='string',
               quantity='number',amount='number',order_time='datetime'),[])}
        self.trace = []

    def check(self):
        if time.monotonic() > self.deadline:
            raise PlanError('조회 실행 시간이 초과되었습니다. 기간이나 조건을 좁혀 주세요.')

    def column(self,table,column):
        if column not in table.columns:
            raise PlanError(f'존재하지 않는 칼럼 {column}. 사용 가능: {", ".join(table.columns)}')
        return table.columns[column]

    def reference(self,ref):
        table = self.tables[ref.step]
        self.column(table,ref.column)
        return [row[ref.column] for row in table.rows]

    def range(self,period):
        now = self.now
        midnight = now.replace(hour=0,minute=0,second=0,microsecond=0)
        if period.kind == 'custom':
            start,end = kst(period.start),min(kst(period.end),now)
            if start > end:
                raise PlanError('미래 기간은 조회할 수 없습니다.')
            return start,end,False
        if period.kind == 'today':
            return midnight,now,True
        if period.kind == 'yesterday':
            return midnight-timedelta(days=1),midnight,False
        if period.kind == 'thisweek':
            return midnight-timedelta(days=midnight.weekday()),now,True
        if period.kind == 'thismonth':
            return midnight.replace(day=1),now,True
        durations = {'last5m':300,'last1h':3600,'last24h':86400,'last7d':7*86400,'last30d':30*86400}
        if period.kind in durations:
            return now-timedelta(seconds=durations[period.kind]),now,True
        return None,now,True

    def predicate(self,table,predicate):
        kind = self.column(table,predicate.column)
        values = self.reference(predicate.ref) if predicate.ref else predicate.values
        def convert(value):
            if value is None:
                return None
            if kind == 'number':
                try:
                    parsed = Decimal(str(value))
                    if not parsed.is_finite():
                        raise ValueError()
                    return parsed
                except Exception as exc:
                    raise PlanError('숫자 필터 값을 지정하세요.') from exc
            if kind == 'datetime':
                try:
                    return kst(value if isinstance(value,datetime) else datetime.fromisoformat(str(value)))
                except ValueError as exc:
                    raise PlanError('시간 필터는 ISO 한국 시각으로 지정하세요.') from exc
            if not isinstance(value,str):
                raise PlanError('문자 칼럼 필터에는 문자열 값이 필요합니다.')
            return value
        converted = [convert(value) for value in values]
        op = predicate.operator
        if op == 'contains' and kind != 'string':
            raise PlanError('contains는 문자 칼럼에서만 사용합니다.')
        if op not in {'in','not_in','is_null','not_null'} and len(converted) != 1:
            raise PlanError('단일값 필터의 참조 결과는 한 값이어야 합니다. ID 목록은 in을 사용하세요.')
        choices = set(converted)
        def matches(row):
            actual = row[predicate.column]
            if op == 'is_null':return actual is None
            if op == 'not_null':return actual is not None
            if op == 'in':return actual in choices
            if op == 'not_in':return actual not in choices
            target = converted[0]
            if op == 'eq':return actual == target
            if op == 'ne':return actual != target
            if actual is None or target is None:return False
            if op == 'contains':
                if kind != 'string':raise PlanError('contains는 문자 칼럼에서만 사용합니다.')
                return target in actual
            if op == 'gt':return actual > target
            if op == 'ge':return actual >= target
            if op == 'lt':return actual < target
            return actual <= target
        return matches

    def aggregate(self,rows,spec):
        if spec.column == '*':
            return len(rows)
        values = [row[spec.column] for row in rows if row[spec.column] is not None]
        if spec.function == 'count':return len(values)
        if spec.function == 'count_distinct':return len(set(values))
        if spec.function == 'sum':return sum((number(v) for v in values),Decimal(0))
        if not values:return None
        if spec.function == 'avg':return sum((number(v) for v in values),Decimal(0))/len(values)
        if spec.function == 'min':return min(values)
        return max(values)

    def query(self,step,source):
        columns = dict(source.columns)
        rows = source.rows
        notes = []
        period = step.period or (Period() if step.source == 'orders' else None)
        left,right,inclusive = None,self.now,True
        if period:
            if self.column(source,step.time_field) != 'datetime':
                raise PlanError('기간 필터에는 datetime 칼럼이 필요합니다.')
            left,right,inclusive = self.range(period)
            rows = [r for r in rows if r[step.time_field] is not None and (left is None or r[step.time_field] >= left)
                    and (r[step.time_field] <= right if inclusive else r[step.time_field] < right)]
            notes.append(f'기간 {period.kind}: {left.isoformat() if left else "전체 과거"}부터 {right.isoformat()}까지. 종료 {"포함" if inclusive else "제외"}.')
        for predicate in step.filters:
            matches = self.predicate(Table(rows,columns,[]),predicate)
            rows = [row for row in rows if matches(row)]
            values = self.reference(predicate.ref) if predicate.ref else predicate.values
            notes.append(f'필터 {predicate.column} {predicate.operator}: {public(values[:8])}'+
                         (f' 외 {len(values)-8}개 값.' if len(values)>8 else '.'))
            self.check()
        if step.time_bucket:
            if self.column(source,step.time_field) != 'datetime':
                raise PlanError('time_bucket에는 datetime 칼럼이 필요합니다.')
            seconds = {'15seconds':15,'minute':60,'5minutes':300,'hour':3600,'day':86400}[step.time_bucket]
            rows = [row for row in rows if row[step.time_field] is not None]
            rows = [dict(row,time_bucket=datetime.fromtimestamp(
                math.floor((row[step.time_field].timestamp()+32400)/seconds)*seconds-32400,KST)) for row in rows]
            columns['time_bucket'] = 'datetime'
        working = Table(rows,columns,[])
        if step.aggregates:
            for field in step.group_by:
                self.column(working,field)
            aliases = [a.alias for a in step.aggregates]
            if len(aliases) != len(set(aliases)) or set(aliases)&set(step.group_by):
                raise PlanError('집계 alias는 서로 다르고 group_by와 겹치지 않아야 합니다.')
            result_columns = {field:columns[field] for field in step.group_by}
            for spec in step.aggregates:
                kind = 'number' if spec.column == '*' else self.column(working,spec.column)
                if spec.function in {'sum','avg'} and kind != 'number':
                    raise PlanError('sum/avg는 숫자 칼럼에서만 사용합니다.')
                result_columns[spec.alias] = 'number' if spec.function in {'sum','count','avg','count_distinct'} else kind
            groups = defaultdict(list)
            for index,row in enumerate(rows):
                if index%1024 == 0:self.check()
                groups[tuple(row[field] for field in step.group_by)].append(row)
                if len(groups)>MAX_ROWS:raise PlanError('집계 그룹이 너무 많습니다. 기간·그룹을 좁혀 주세요.')
            if not step.group_by and not groups:groups[()] = []
            if step.fill_gaps:
                if period is None or left is None:
                    raise PlanError('빈 시간 구간을 채우려면 전체가 아닌 명시적 기간이 필요합니다.')
                first = math.floor((left.timestamp()+32400)/seconds)*seconds-32400
                last = math.floor((right.timestamp()+32400)/seconds)*seconds-32400
                if not inclusive and right.timestamp()==last:last -= seconds
                count = max(0,int((last-first)//seconds)+1)
                if count>MAX_BINS:raise PlanError('시간 구간이 너무 많습니다. 집계 간격을 늘려 주세요.')
                index = step.group_by.index('time_bucket')
                others = {key[:index]+key[index+1:] for key in groups} or ({()} if len(step.group_by)==1 else set())
                if count*len(others)>MAX_ROWS:raise PlanError('시간·고객 그룹이 너무 많습니다.')
                for other in others:
                    for slot in range(int(first),int(last)+1,seconds):
                        key = other[:index]+(datetime.fromtimestamp(slot,KST),)+other[index:]
                        groups.setdefault(key,[])
                notes.append('주문이 없는 시간 구간의 sum/count는 0, avg/min/max는 null입니다.')
            rows = [dict(zip(step.group_by,key),**{spec.alias:self.aggregate(group,spec) for spec in step.aggregates})
                    for key,group in groups.items()]
            columns = result_columns
        working = Table(rows,columns,[])
        for spec in step.order_by:
            self.column(working,spec.column)
        for spec in reversed(step.order_by):
            present = [r for r in rows if r[spec.column] is not None]
            absent = [r for r in rows if r[spec.column] is None]
            rows = sorted(present,key=lambda r:r[spec.column],reverse=spec.direction=='desc')+absent
        before = len(rows)
        if step.limit and len(rows)>step.limit:
            selected = rows[:step.limit]
            if step.keep_ties:
                cutoff = tuple(selected[-1][s.column] for s in step.order_by)
                selected += [r for r in rows[step.limit:] if tuple(r[s.column] for s in step.order_by)==cutoff]
            rows = selected
            notes.append(f'정렬 후 상위 {step.limit}행 선택. 선택 전 {before}행, 동률 포함 {str(step.keep_ties).lower()}, 선택 후 {len(rows)}행.')
        if step.select:
            for field in step.select:self.column(working,field)
            rows = [{field:row[field] for field in step.select} for row in rows]
            columns = {field:columns[field] for field in step.select}
        return Table(rows,columns,notes)

    def join(self,step,left):
        right = self.tables[step.right]
        for key in step.on:
            if self.column(left,key.left) != self.column(right,key.right):
                raise PlanError('join 키의 자료형이 다릅니다.')
        mapping = {}
        columns = dict(left.columns)
        for field,kind in right.columns.items():
            target = field if field not in columns else 'right_'+field
            if target in columns:raise PlanError('join 결과 칼럼이 겹칩니다. select로 칼럼을 좁혀 주세요.')
            mapping[field]=target;columns[target]=kind
        index = defaultdict(list)
        for row in right.rows:
            key = tuple(row[k.right] for k in step.on)
            if None not in key:index[key].append(row)
        rows = []
        for count,row in enumerate(left.rows):
            if count%1024 == 0:self.check()
            key = tuple(row[k.left] for k in step.on)
            matches = index.get(key,[]) if None not in key else []
            if not matches and step.how == 'left':matches = [None]
            for match in matches:
                rows.append(dict(row,**{target:match[field] if match else None for field,target in mapping.items()}))
                if len(rows)>MAX_ROWS:raise PlanError('join 결과가 너무 많습니다. 먼저 집계하거나 조건을 좁혀 주세요.')
        return Table(rows,columns,['오른쪽의 중복 칼럼 이름에는 right_ 접두어를 붙입니다.'])

    def compare(self,step,current):
        previous = self.tables[step.right]
        for key in step.keys:
            if self.column(current,key) != self.column(previous,key):raise PlanError('비교 키의 자료형이 다릅니다.')
        for metric in step.metrics:
            if self.column(current,metric) != 'number' or self.column(previous,metric) != 'number':
                raise PlanError('비교 지표는 양쪽 결과의 숫자 칼럼이어야 합니다.')
            if any(f'{prefix}_{metric}' in step.keys for prefix in ('current','previous','delta','delta_pct')):
                raise PlanError('비교 키가 자동 생성되는 결과 칼럼 이름과 겹칩니다.')
        def index(table):
            result = {}
            for row in table.rows:
                key = tuple(row[k] for k in step.keys)
                if key in result:raise PlanError('비교 키별 결과가 여러 행입니다. 먼저 같은 키로 집계하세요.')
                result[key]=row
            return result
        current_index,previous_index = index(current),index(previous)
        keys = list(dict.fromkeys([*current_index,*previous_index]))
        rows = []
        columns = {key:current.columns[key] for key in step.keys}
        for key in keys:
            row = dict(zip(step.keys,key))
            for metric in step.metrics:
                c = number(current_index.get(key,{}).get(metric,0))
                p = number(previous_index.get(key,{}).get(metric,0))
                row.update({f'current_{metric}':c,f'previous_{metric}':p,
                            f'delta_{metric}':c-p if c is not None and p is not None else None,
                            f'delta_pct_{metric}':(c-p)/abs(p)*100 if c is not None and p else None})
                for prefix in ('current','previous','delta','delta_pct'):columns[f'{prefix}_{metric}']='number'
            rows.append(row)
        return Table(rows,columns,['source는 현재/분자 구간, right는 기준/이전 구간입니다. 없는 그룹은 0, 기준값이 0이면 증감률은 null입니다.'])

    def calculate(self,step,source):
        rows,columns = [dict(r) for r in source.rows],dict(source.columns)
        for calculation in step.calculations:
            if calculation.alias in columns:raise PlanError('계산 alias는 기존 칼럼과 달라야 합니다.')
            def operand(spec):
                if spec.column:
                    if self.column(Table(rows,columns,[]),spec.column) != 'number':raise PlanError('계산에는 숫자 칼럼이 필요합니다.')
                    return lambda row:number(row[spec.column])
                if spec.ref:
                    values = self.reference(spec.ref)
                    if len(values)!=1:raise PlanError('계산 ref는 한 행의 숫자여야 합니다. 먼저 전체 합계를 집계하세요.')
                    value = number(values[0])
                else:value = number(spec.value)
                return lambda _:value
            left,right = operand(calculation.left),operand(calculation.right)
            for row in rows:
                a,b = left(row),right(row)
                value = None
                if a is not None and b is not None:
                    if calculation.function == 'add':value = a+b
                    elif calculation.function == 'subtract':value = a-b
                    elif calculation.function == 'multiply':value = a*b
                    elif b:value = a/b*(100 if calculation.function=='percent' else 1)
                row[calculation.alias]=value
            columns[calculation.alias]='number'
        return Table(rows,columns,['0으로 나누거나 입력 값이 null이면 계산 결과는 null입니다.'])

    def window(self,step,source):
        for key in step.partition_by:self.column(source,key)
        for sort in step.window_order_by:self.column(source,sort.column)
        aliases = [spec.alias for spec in step.windows]
        if len(aliases)!=len(set(aliases)) or set(aliases)&set(source.columns):
            raise PlanError('window alias는 서로 다르고 기존 칼럼과 겹치지 않아야 합니다.')
        columns = dict(source.columns)
        for spec in step.windows:
            kind = self.column(source,spec.column) if spec.column else 'number'
            if spec.function=='running_sum' and kind!='number':raise PlanError('running_sum은 숫자 칼럼에서만 사용합니다.')
            columns[spec.alias] = kind if spec.function=='lag' else 'number'
        groups = defaultdict(list)
        for row in source.rows:groups[tuple(row[key] for key in step.partition_by)].append(row)
        result = []
        for group in groups.values():
            self.check()
            for sort in reversed(step.window_order_by):
                present = [r for r in group if r[sort.column] is not None]
                absent = [r for r in group if r[sort.column] is None]
                group = sorted(present,key=lambda r:r[sort.column],reverse=sort.direction=='desc')+absent
            running = {spec.alias:Decimal(0) for spec in step.windows if spec.function=='running_sum'}
            previous_key,rank = None,0
            for index,row in enumerate(group):
                key = tuple(row[sort.column] for sort in step.window_order_by)
                if index==0 or key!=previous_key:rank=index+1
                previous_key=key
                output = dict(row)
                for spec in step.windows:
                    if spec.function=='lag':value=group[index-spec.offset][spec.column] if index>=spec.offset else None
                    elif spec.function=='row_number':value=index+1
                    elif spec.function=='rank':value=rank
                    else:
                        running[spec.alias]+=number(row[spec.column]) or Decimal(0)
                        value=running[spec.alias]
                    output[spec.alias]=value
                result.append(output)
        return Table(result,columns,['partition별 지정 순서로 계산합니다. lag의 첫 구간은 null이며 running_sum은 각 행까지의 누적 합계입니다.'])

    def run(self,plan):
        if plan.status != 'ready':raise PlanError('실행 가능한 계획이 아닙니다.')
        for step in plan.steps:
            self.check()
            try:
                source = self.tables[step.source]
                table = getattr(self,step.operation)(step,source)
                if len(table.rows)>MAX_ROWS:raise PlanError(f'중간 결과는 {MAX_ROWS}행까지입니다. 집계/필터/limit을 사용하세요.')
                self.tables[step.id]=table
                self.trace.append(dict(id=step.id,title=step.title,operation=step.operation,
                                       source=step.source,row_count=len(table.rows),columns=table.columns,
                                       notes=table.notes))
            except PlanError as exc:
                available = {name:dict(self.tables[name].columns) for name in
                             dict.fromkeys([step.source,getattr(step,'right',None)]+[p.ref.step for p in getattr(step,'filters',[]) if p.ref])
                             if name is not None}
                hints = []
                for name,columns in available.items():
                    producer = next((s for s in plan.steps if s.id == name),None)
                    if producer and getattr(producer,'aggregates',[]):
                        hints.append(f'{name}은 집계 결과입니다. 남는 칼럼은 group_by와 집계 alias뿐입니다. '
                                     '고객별 결과가 필요하면 원본 집계 단계 group_by에 customer_id를, '
                                     '제품별 결과가 필요하면 product_id를 유지하세요.')
                diagnostics = dict(failed_step=step.id,operation=step.operation,
                                   available_columns=available,completed_steps=self.trace,hints=hints)
                raise PlanError(f'{step.id} ({step.title}): {exc}',diagnostics) from exc
        return self.tables[plan.output]

    def evidence(self,plan):
        final = self.tables[plan.output]
        def preview(step_id,limit):
            table = self.tables[step_id]
            return [dict(evidence_id=f'{step_id}-R{i}',**public(row)) for i,row in enumerate(table.rows[:limit],1)]
        steps = [dict(trace,rows=preview(trace['id'],8),truncated=trace['row_count']>8) for trace in self.trace]
        return dict(as_of=self.now.isoformat(),timezone='Asia/Seoul',output=plan.output,
                    rows=preview(plan.output,30),row_count=len(final.rows),truncated=len(final.rows)>30,
                    columns=final.columns,steps=steps,
                    note='한 질문의 모든 단계는 같은 시각·같은 주문 스냅샷을 사용합니다. 표는 일부 행일 수 있으며 다음 단계는 중간 결과 전체를 사용합니다.')


def execute(plan,orders,now):
    executor = Executor(orders,now)
    executor.run(plan)
    return executor.evidence(plan)
