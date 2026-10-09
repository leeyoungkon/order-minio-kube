import asyncio,json,threading
from datetime import datetime,timezone
from decimal import Decimal
from io import BytesIO
import httpx,pytest,pyarrow as pa,pyarrow.parquet as pq
from fastapi.testclient import TestClient
from app import create_app,Ollama,Question,plan_and_execute
from query_engine import PipelinePlan,execute
from conversation import Conversation
from order_data import Settings,ParquetStore
from test_engine import NOW,rows,q,agg,plan,order,EXAMPLES

@pytest.fixture(autouse=True)
def fixed_snapshot_clock(monkeypatch):
 class Clock(datetime):
  @classmethod
  def now(cls,tz=None):return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
 monkeypatch.setattr('app.datetime',Clock)

class Store:
 def __init__(self,data=None,error=None,success=True):
  self.rows=tuple(rows() if data is None else data);self.state=dict(last_success=datetime.now(timezone.utc).isoformat() if success else None,error=error,file_count=1,order_count=len(self.rows),generation=1)
 def snapshot(self):return self.rows,dict(self.state)
class LLM:
 model='gemma3:12b'
 def __init__(self,payload=None):self.payload=payload or EXAMPLES[0]['plan'];self.calls=[];self.explains=0
 async def models(self):return [self.model]
 async def plan(self,request,metadata,correction=''):self.calls.append((metadata,correction));return json.dumps(self.payload,ensure_ascii=False)
 async def explain(self,question,evidence):self.explains+=1;return '검증용 근거 답변 [s1-R1] [s5-R1]'

def test_chat_multi_step_evidence_and_signed_context():
 llm=LLM()
 with TestClient(create_app(Store(),llm,start_poller=False)) as c:
  r=c.post('/api/chat',json={'question':EXAMPLES[0]['question']});assert r.status_code==200
  v=r.json();assert v['evidence']['rows'][0]['product_id']=='P002' and len(v['evidence']['steps'])==5 and v['context_token']
  c.post('/api/chat',json={'question':'그 고객의 주문은?','context_token':v['context_token']})
  assert llm.calls[-1][0]['previous_results'][-1]['rows'][0]['customer_id']=='C001'
  assert c.get('/healthz').json()['version']=='2.0.1' and c.get('/api/status').json()['ollama_error'] is None
  assert len(c.get('/api/catalog').json()['examples'])==8 and '단계별 조회 근거' in c.get('/').text

def test_json_api_without_llm_and_invalid_query():
 llm=LLM()
 with TestClient(create_app(Store(),llm,start_poller=False)) as c:
  r=c.post('/api/query',json={'plan':EXAMPLES[0]['plan']});assert r.status_code==200 and not llm.calls
  bad=plan([q('s1',order_by=[{'column':'nonexistent'}])]).model_dump(mode='json')
  assert c.post('/api/query',json={'plan':bad}).status_code==400
  assert c.post('/api/query',json={'plan':{'steps':[{'id':'s','title':'s','operation':'sql'}],'output':'s'}}).status_code==422

def test_json_and_runtime_errors_repair_full_plan():
 class Repair(LLM):
  async def plan(self,request,metadata,correction=''):
   self.calls.append((metadata,correction))
   if len(self.calls)==1:return '{"steps":[],"output":null}'
   if len(self.calls)==2:return json.dumps(dict(steps=[q('s1',aggregates=[agg('sum','nonexistent','amount')])],output='s1'))
   return json.dumps(EXAMPLES[0]['plan'])
 llm=Repair()
 with TestClient(create_app(Store(),llm,start_poller=False)) as c:
  assert c.post('/api/chat',json={'question':'질문'}).status_code==200
  assert len(llm.calls)==3 and 'steps' in llm.calls[1][1] and 'nonexistent' in llm.calls[2][1]

def test_invalid_after_repairs_is_error_not_answer():
 llm=LLM({'steps':[],'output':None})
 with TestClient(create_app(Store(),llm,start_poller=False)) as c:
  assert c.post('/api/chat',json={'question':'질문'}).status_code==422 and len(llm.calls)==3 and llm.explains==0

@pytest.mark.parametrize('status',['unsupported','clarify'])
def test_refusal_or_clarification_without_explanation(status):
 llm=LLM(dict(status=status,reason='기간 확인',steps=[],output=None))
 with TestClient(create_app(Store(),llm,start_poller=False)) as c:
  r=c.post('/api/chat',json={'question':'질문'}).json();assert r['answer']=='기간 확인' and r['evidence'] is None and llm.explains==0

def test_empty_does_not_invent_winner():
 llm=LLM()
 with TestClient(create_app(Store(data=[]),llm,start_poller=False)) as c:
  r=c.post('/api/chat',json={'question':'질문'}).json();assert '없습니다' in r['answer'] and r['evidence']['row_count']==0 and llm.explains==0

def test_explanation_failure_keeps_calculations():
 class Failing(LLM):
  async def explain(self,*_):raise RuntimeError('GPU 오류')
 with TestClient(create_app(Store(),Failing(),start_poller=False)) as c:
  r=c.post('/api/chat',json={'question':'질문'});assert r.status_code==200
  assert r.json()['evidence']['rows'][0]['product_id']=='P002' and '설명 생성 실패' in r.json()['warning']

def test_initial_failure_and_stale_snapshot_warning():
 with TestClient(create_app(Store(success=False,error='MinIO 오류'),LLM(),start_poller=False)) as c:assert c.post('/api/chat',json={'question':'질문'}).status_code==503
 with TestClient(create_app(Store(error='갱신 오류'),LLM(),start_poller=False)) as c:assert '스냅샷' in c.post('/api/chat',json={'question':'질문'}).json()['warning']

def test_real_ollama_request_schema_examples_and_explanation():
 sent=[]
 async def run():
  async def handler(r):
   if r.method=='GET':return httpx.Response(200,json={'models':[{'name':'gemma3:12b'}]})
   value=json.loads(r.content);sent.append(value);text=json.dumps(EXAMPLES[0]['plan']) if value.get('format') else '근거 [s5-R1]'
   return httpx.Response(200,json={'message':{'content':text}})
  async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
   llm=Ollama(c,'http://ollama:11434','gemma3:12b');assert await llm.models()==['gemma3:12b']
   p,e=await plan_and_execute(llm,Question(question=EXAMPLES[0]['question']),rows(),NOW,[])
   assert p.output=='s5' and await llm.explain('질문',e)=='근거 [s5-R1]'
 asyncio.run(run())
 assert sent[0]['format']['properties']['steps'] and sent[0]['options']['num_ctx']==16384 and sent[0]['stream'] is False
 assert 'examples:' in sent[0]['messages'][0]['content'] and 'previous_results' in sent[0]['messages'][0]['content'] and 'format' not in sent[1]

def test_missing_model_error():
 async def run():
  async def handler(_):return httpx.Response(404,json={'error':"model 'gemma3:12b' not found"})
  async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
   with pytest.raises(RuntimeError,match='not found'):await Ollama(c,'http://ollama','gemma3:12b').chat('질문')
 asyncio.run(run())

def test_timeout_releases_lock(monkeypatch):
 monkeypatch.setenv('CHAT_TIMEOUT','0.01')
 class Slow(LLM):
  async def plan(self,*_):await asyncio.sleep(1)
 with TestClient(create_app(Store(),Slow(),start_poller=False)) as c:
  assert c.post('/api/chat',json={'question':'질문'}).status_code==504 and c.get('/api/status').json()['busy'] is False

def test_concurrent_query_gets_busy():
 entered,released=threading.Event(),threading.Event()
 class Slow(LLM):
  async def plan(self,*args):entered.set();await asyncio.to_thread(released.wait,5);return await super().plan(*args)
 with TestClient(create_app(Store(),Slow(),start_poller=False)) as c:
  replies=[];worker=threading.Thread(target=lambda:replies.append(c.post('/api/chat',json={'question':'첫 질문'})));worker.start()
  try:
   assert entered.wait(2) and c.post('/api/chat',json={'question':'둘째'}).status_code==429
   assert c.post('/api/query',json={'plan':EXAMPLES[0]['plan']}).status_code==429
  finally:released.set();worker.join(5)
  assert replies[0].status_code==200

def test_context_signature_other_session_and_expiry(monkeypatch):
 signer=Conversation();token=signer.write([],EXAMPLES[0]['question'],execute(PipelinePlan.model_validate(EXAMPLES[0]['plan']),rows(),NOW))
 assert signer.read(token)[0][0]['rows'][0]['customer_id']=='C001' and signer.read(token+'x')[0]==[] and Conversation().read(token)[1]
 monkeypatch.setattr('conversation.time.time',lambda:9999999999);assert signer.read(token)[1]

def test_parquet_kst_decimal_latest_revision():
 payloads={}
 for key,value,stamp in [('a.parquet','0.10',NOW),('b.parquet','0.20',NOW.replace(second=1))]:
  b=BytesIO();pq.write_table(pa.Table.from_pylist([dict(order_id='same',customer_id='C001',product_id='P001',quantity=1,total_amount=Decimal(value),order_time=NOW.replace(tzinfo=None),etl_extracted_at=stamp)]),b);payloads[key]=b.getvalue()
 class S3:
  def get_paginator(self,_):return self
  def paginate(self,**_):yield {'Contents':[dict(Key=k,ETag=k,Size=len(v),LastModified=NOW) for k,v in payloads.items()]}
  def get_object(self,**kw):return {'Body':BytesIO(payloads[kw['Key']])}
 store=ParquetStore(Settings('http://sample:9000','sample','sample'),S3());store.refresh();data,state=store.snapshot()
 assert state['order_count']==1 and state['raw_rows']==2 and data[0].amount==Decimal('0.20') and data[0].ordered_at==NOW
 assert execute(plan([q('s1',aggregates=[agg('sum','amount','amount')])]),data,NOW)['rows'][0]['amount']=='0.20'
