import json
from datetime import datetime,timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
import pytest
from pydantic import ValidationError
from query_engine import PipelinePlan,Executor,PlanError,execute
from order_data import Order
NOW=datetime(2026,10,9,16,10,tzinfo=ZoneInfo('Asia/Seoul'))
EXAMPLES=json.loads((Path(__file__).parents[1]/'examples.json').read_text())
def order(id='a',customer='C001',product='P001',quantity=1,amount='60',age=10):return Order(id,customer,product,quantity,Decimal(amount),NOW-timedelta(seconds=age),NOW)
def rows():return [order('a'),order('b',product='P002',quantity=8,amount='40'),order('c',customer='C002',quantity=100,amount='90'),order('d',customer='C003',product='P003',quantity=6,amount='80',age=86400),order('e',customer='C004',product='P004',quantity=2,amount='10'),order('future',customer='C005',amount='1000',age=-60)]
def agg(function,column,alias):return dict(function=function,column=column,alias=alias)
def q(id,**kw):return dict(id=id,title=id,operation='query',**kw)
def plan(steps):return PipelinePlan(steps=steps,output=steps[-1]['id'])
def result(steps,data=None):return execute(plan(steps),rows() if data is None else data,NOW)

def test_customer_then_own_product_uses_five_common_operations():
 e=execute(PipelinePlan.model_validate(EXAMPLES[0]['plan']),rows(),NOW);r=e['rows'][0]
 assert e['row_count']==1 and (r['customer_id'],r['product_id'],r['quantity'],r['amount'])==('C001','P002','8','40')
 assert e['steps'][0]['rows'][0]['amount']=='100' and r['evidence_id']=='s5-R1'
 assert [s['operation'] for s in e['steps']]==['query','query','query','join','query']

def test_product_then_customer_selection():
 r=execute(PipelinePlan.model_validate(EXAMPLES[1]['plan']),rows(),NOW)['rows'][0]
 assert r['customer_id']=='C002' and r['product_id']=='P001' and r['amount']=='90'

def test_top_customer_and_product_ties_are_all_retained():
 data=[order('a',quantity=2,amount='50'),order('b',product='P002',quantity=2,amount='50'),order('c',customer='C002',product='P003',quantity=3,amount='100')]
 e=execute(PipelinePlan.model_validate(EXAMPLES[0]['plan']),data,NOW)
 assert {(r['customer_id'],r['product_id']) for r in e['rows']}=={('C001','P001'),('C001','P002'),('C002','P003')}

def test_decimal_sum_and_scalar_share_reference():
 e=execute(PipelinePlan.model_validate(EXAMPLES[2]['plan']),rows(),NOW)
 assert Decimal(e['rows'][0]['share_pct'])==50 and sum(Decimal(r['share_pct']) for r in e['rows'])==100
 e=result([q('s1',aggregates=[agg('sum','amount','amount')])],[order('a',amount='0.1'),order('b',amount='0.2')])
 assert e['rows'][0]['amount']=='0.3'

def test_period_compare_missing_group_and_zero_baseline():
 e=execute(PipelinePlan.model_validate(EXAMPLES[3]['plan']),rows(),NOW)
 assert e['rows'][0]['customer_id']=='C001' and e['rows'][0]['delta_amount']=='100'
 rs=e['steps'][2]['rows'];a=next(r for r in rs if r['customer_id']=='C001');b=next(r for r in rs if r['customer_id']=='C003')
 assert a['previous_amount']=='0' and a['delta_pct_amount'] is None and b['delta_amount']=='-80'

def test_unseen_question_multiple_distinct_products_composes():
 e=result([q('s1',group_by=['customer_id'],aggregates=[agg('count_distinct','product_id','products')]),q('s2',source='s1',filters=[dict(column='products',operator='ge',values=[2])])])
 assert e['row_count']==1 and e['rows'][0]['customer_id']=='C001'

def test_unseen_question_orders_above_customer_average_composes():
 steps=[q('s1',select=['order_id','customer_id','amount']),q('s2',source='s1',group_by=['customer_id'],aggregates=[agg('avg','amount','average')]),dict(id='s3',title='join',operation='join',source='s1',right='s2',on=[dict(left='customer_id',right='customer_id')]),dict(id='s4',title='gap',operation='calculate',source='s3',calculations=[dict(alias='gap',function='subtract',left={'column':'amount'},right={'column':'average'})]),q('s5',source='s4',filters=[dict(column='gap',operator='gt',values=[0])])]
 e=result(steps);assert e['row_count']==1 and e['rows'][0]['order_id']=='a' and e['rows'][0]['gap']=='10'

def test_reference_uses_all_rows_not_preview():
 data=[order(str(i),customer=f'C{i:03}',amount='10') for i in range(45)]
 e=result([q('s1',group_by=['customer_id'],aggregates=[agg('sum','amount','amount')]),q('s2',filters=[dict(column='customer_id',ref={'step':'s1','column':'customer_id'})],aggregates=[agg('count','*','orders')])],data)
 assert len(e['steps'][0]['rows'])==8 and e['steps'][0]['truncated'] and e['rows'][0]['orders']==45

def test_empty_reference_matches_nothing():
 e=result([q('s1',filters=[dict(column='customer_id',values=['missing'])]),q('s2',filters=[dict(column='customer_id',ref={'step':'s1','column':'customer_id'})],group_by=['product_id'],aggregates=[agg('sum','quantity','quantity')])])
 assert e['row_count']==0

def test_empty_totals_are_zero_and_average_null():
 r=result([q('s1',filters=[dict(column='customer_id',values=['missing'])],aggregates=[agg('sum','amount','amount'),agg('count','*','orders'),agg('avg','amount','average')])])['rows'][0]
 assert r['amount']=='0' and r['orders']==0 and r['average'] is None

def test_kst_midnight_current_cutoff_and_custom_exclusive_end():
 midnight=NOW.replace(hour=0,minute=0,second=0,microsecond=0);age=int((NOW-midnight).total_seconds())
 data=[order('old',age=age+1),order('midnight',age=age),order('now',age=0),order('future',age=-1)]
 def count(period):return result([q('s1',period=period,aggregates=[agg('count','*','orders')])],data)['rows'][0]['orders']
 assert count({'kind':'yesterday'})==1 and count({'kind':'today'})==2
 assert count({'kind':'custom','start':midnight.replace(tzinfo=None).isoformat(),'end':NOW.replace(tzinfo=None).isoformat()})==1
 with pytest.raises(PlanError,match='미래'):count({'kind':'custom','start':(NOW+timedelta(hours=1)).isoformat(),'end':(NOW+timedelta(hours=2)).isoformat()})

def test_dense_time_buckets_and_daily_kst_boundaries():
 e=execute(PipelinePlan.model_validate(EXAMPLES[5]['plan']),rows(),NOW)
 assert e['row_count']==13 and e['rows'][-1]['time_bucket']=='2026-10-09T16:10:00+09:00'
 assert e['rows'][-2]['quantity']=='2' and e['rows'][-1]['quantity']=='0'
 e=result([q('s1',period={'kind':'last7d'},time_bucket='day',group_by=['time_bucket'],aggregates=[agg('count','*','orders')])])
 assert all(datetime.fromisoformat(r['time_bucket']).hour==0 for r in e['rows'])

def test_left_join_and_zero_division_nulls():
 steps=[q('s1',group_by=['customer_id'],aggregates=[agg('sum','amount','amount')]),q('s2',source='s1',filters=[dict(column='customer_id',values=['C001'])]),dict(id='s3',title='left',operation='join',source='s1',right='s2',how='left',on=[dict(left='customer_id',right='customer_id')]),dict(id='s4',title='divide',operation='calculate',source='s3',calculations=[dict(alias='ratio',function='divide',left={'column':'amount'},right={'value':0})])]
 e=result(steps);assert all(r['ratio'] is None for r in e['rows'])
 assert next(r for r in e['rows'] if r['customer_id']=='C002')['right_amount'] is None

def test_scalar_ref_and_comparison_keys_must_be_unique():
 with pytest.raises(PlanError,match='한 행'):result([q('s1'),dict(id='s2',title='bad',operation='calculate',source='s1',calculations=[dict(alias='ratio',function='divide',left={'column':'amount'},right={'ref':{'step':'s1','column':'amount'}})])])
 with pytest.raises(PlanError,match='먼저.*집계'):result([q('s1'),dict(id='s2',title='bad',operation='compare',source='s1',right='s1',keys=['customer_id'],metrics=['amount'])])

@pytest.mark.parametrize('step',[
 q('s1',filters=[dict(column='missing',values=['x'])]),q('s1',aggregates=[agg('sum','product_id','total')]),
 q('s1',aggregates=[agg('sum','amount','total')],order_by=[dict(column='amount')]),q('s1',filters=[dict(column='amount',values=['nan'])]),
 q('s1',filters=[dict(column='quantity',operator='contains',values=[1])]),q('s1',group_by=['customer_id'],aggregates=[agg('sum','amount','customer_id')]),
 q('s1',time_bucket='minute',fill_gaps=True,period={'kind':'last30d'},group_by=['time_bucket'],aggregates=[agg('count','*','orders')])])
def test_runtime_validation(step):
 with pytest.raises(PlanError):result([step])

@pytest.mark.parametrize('payload',[
 {'steps':[dict(id='s1',title='SQL',operation='sql',sql='DROP TABLE orders')],'output':'s1'},
 {'steps':[q('s1',source='future')],'output':'s1'},{'steps':[q('s1'),q('s1')],'output':'s1'},
 {'steps':[q('orders')],'output':'orders'},{'steps':[q('s1',filters=[dict(column='customer_id',ref={'step':'s1','column':'customer_id'})])],'output':'s1'},
 {'steps':[q('s1',limit=501)],'output':'s1'},{'steps':[q('s1',keep_ties=True,limit=1)],'output':'s1'},
 {'steps':[q('s1',aggregates=[agg('sum','amount','evidence_id')])],'output':'s1'},
 {'steps':[dict(id='s1',title='join',operation='join',right='orders')],'output':'s1'},
 {'steps':[dict(id='s1',title='compare',operation='compare',right='orders',metrics=['amount'],filters=[dict(column='quantity',values=[1])])],'output':'s1'},
 {'status':'unsupported','reason':'없음','steps':[q('s1')],'output':'s1'}, {'steps':[q('s1')]*11,'output':'s1'}])
def test_json_validation(payload):
 with pytest.raises(ValidationError):PipelinePlan.model_validate(payload)

def test_sql_is_literal_only_and_result_limits_are_explicit():
 assert result([q('s1',filters=[dict(column='customer_id',values=['DROP TABLE orders'])])])['row_count']==0
 data=[order(str(i)) for i in range(20001)]
 with pytest.raises(PlanError,match='중간 결과'):result([q('s1')],data)
 assert result([q('s1',aggregates=[agg('count','*','orders')])],data)['rows'][0]['orders']==20001
 with pytest.raises(PlanError,match='시간'):Executor(rows(),NOW,seconds=-1).run(plan([q('s1')]))
 e=result([q('s1')],data[:50]);assert e['row_count']==50 and len(e['rows'])==30 and e['truncated']

def test_window_customer_lag_and_cumulative_then_delta():
 data=[order('a',quantity=2,age=80),order('b',quantity=3,age=20),order('c',customer='C002',quantity=7,age=15)]
 steps=[q('s1',select=['customer_id','order_time','quantity']),dict(id='s2',title='window',operation='window',source='s1',partition_by=['customer_id'],window_order_by=[dict(column='order_time',direction='asc')],windows=[dict(function='lag',column='quantity',alias='previous'),dict(function='running_sum',column='quantity',alias='cumulative')]),dict(id='s3',title='delta',operation='calculate',source='s2',calculations=[dict(alias='delta',function='subtract',left={'column':'quantity'},right={'column':'previous'}),dict(alias='delta_pct',function='percent',left={'column':'delta'},right={'column':'previous'})])]
 e=result(steps,data);a=[r for r in e['rows'] if r['customer_id']=='C001'];b=next(r for r in e['rows'] if r['customer_id']=='C002')
 assert a[0]['previous'] is None and a[0]['delta'] is None and a[0]['cumulative']=='2'
 assert a[1]['previous']==2 and a[1]['cumulative']=='5' and a[1]['delta']=='1' and Decimal(a[1]['delta_pct'])==50
 assert b['cumulative']=='7' and b['previous'] is None

def test_window_rank_ties_row_number_and_long_generated_columns():
 steps=[q('s1',group_by=['customer_id'],aggregates=[agg('sum','amount','amount')]),dict(id='s2',title='rank',operation='window',source='s1',window_order_by=[dict(column='amount')],windows=[dict(function='rank',alias='rank'),dict(function='row_number',alias='ordinal')])]
 e=result(steps,[order('a',amount='50'),order('b',customer='C002',amount='50'),order('c',customer='C003',amount='20')]);assert [r['rank'] for r in e['rows']]==[1,1,3] and [r['ordinal'] for r in e['rows']]==[1,2,3]
 name='very_long_metric_for_customer_amount'
 steps=[q('s1',group_by=['customer_id'],aggregates=[agg('sum','amount',name)]),dict(id='s2',title='compare',operation='compare',source='s1',right='s1',keys=['customer_id'],metrics=[name]),q('s3',source='s2',order_by=[dict(column='delta_pct_'+name)])]
 assert result(steps)['row_count']==3

@pytest.mark.parametrize('payload',[
 {'steps':[q('s1'),dict(id='s2',title='bad',operation='calculate',calculations=[dict(alias='x',function='add',left={'value':1},right={'value':2})])],'output':'s2'},
 {'steps':[q('s1',filters=[dict(column='customer_id',ref={'step':'orders','column':'customer_id'})])],'output':'s1'},
 {'steps':[q('s1'),dict(id='s2',title='bad',operation='window',source='s1',windows=[dict(function='lag',column='quantity',alias='last')])],'output':'s2'},
 {'steps':[q('s1',windows=[dict(function='row_number',alias='rank')])],'output':'s1'},
])
def test_raw_source_refs_and_window_schema_rejected(payload):
 with pytest.raises(ValidationError):PipelinePlan.model_validate(payload)

def test_window_existing_alias_and_nonnumeric_running_sum_fail():
 for alias,column in [('quantity','quantity'),('running','customer_id')]:
  with pytest.raises(PlanError):result([q('s1'),dict(id='s2',title='window',operation='window',source='s1',window_order_by=[dict(column='order_time')],windows=[dict(function='running_sum',column=column,alias=alias)])])
