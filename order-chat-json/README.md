# order-chat 2.0.1: Gemma + JSON 다단계 주문 질의

## 2.0.1 계획 검증 오류 수정

실제 화면에 나온 `s3: 존재하지 않는 칼럼 customer_id. 사용 가능: amount` 오류를 재현하고 수정했습니다.
고객별 비교 계획인데 오늘/어제 집계에 group_by가 누락된 경우, 비교 keys를 집계에 보존합니다.
이 보정은 해당 비교에만 사용되는 원본 집계이며 select/limit/시간 버킷/기존 group_by가 없는 경우에만 적용합니다.
다른 계산에서 전체 합계로 재사용되는 결과나 알 수 없는 칼럼은 자동으로 의미를 변경하지 않습니다.
보정 내역은 응답 evidence.plan_repairs와 화면의 단계별 조회 근거에 표시됩니다.

다른 검증 실패에는 source/right의 실제 칼럼·완료 단계·집계 키 유지 안내를 모델에 전달합니다.
오류만 반복하는 대신 직전 전체 계획과 구체적인 수정 정보를 제공하고 최대 두 번 교정합니다.
질문마다 예시 8개 전체 대신 관련성이 높은 2개를 모델에 보내며, JSON Schema의 설명용 title/default를 줄입니다.
연산·자료형·칼럼·참조 검증은 유지합니다. 계획 실패 횟수와 오류는 Pod 로그에 출력됩니다.
전체 계획 디버깅이 필요하면 ORDER_CHAT_DEBUG_PLANS=true로 설정하고 재시작하세요.
디버그 로그에는 질의 계획과 데이터 식별자가 포함되므로 필요할 때만 켜세요.

수정 검증: 화면의 고객별 비교 오류, 전체 합계 비교 유지, 재사용된 분모/상위 제한/projection 보호,
스키마 보존, API·집계 회귀 테스트를 포함한 68개 테스트가 통과했습니다.
테스트의 모델 응답은 모의 응답입니다. 실제 설치된 Gemma의 계획 생성 성공률은 별도로 확인해야 합니다.

220.149.119.234 Kubernetes의 설치된 Ollama/Gemma와 기존 MinIO 주문 Parquet를 사용합니다.
질문 화면은 NodePort **30112**이며, 기존 그래프 대시보드 30111과 함께 사용할 수 있습니다.

## 무엇이 달라졌나

Gemma가 질문을 최대 10단계의 JSON 계획으로 변환합니다. Python 실행기는
범용 연산 query / join / compare / calculate / window를 순서대로 실행하고
앞 단계 결과 전체를 다음 단계의 조건이나 데이터로 넘깁니다.
질문 문구를 인식해 특정 고객/품목 전용 함수로 보내는 규칙은 없습니다.
Gemma는 실제 실행 결과를 근거로 설명하고, 화면에는 최종 결과와 단계별 근거표를 표시합니다.

- query: 기간·고객·제품·집계값 필터, 그룹별 합계/평균/최솟값/최댓값/건수/서로 다른 값 개수, 정렬, 상위 선택, 동률.
- join: 앞 단계 두 결과를 하나 또는 여러 키로 연결. 고객별 최다 품목처럼 여러 그룹의 조건을 맞출 때 사용.
- compare: 서로 다른 두 기간의 고객·제품별 금액/수량/건수 및 증감·증감률 비교.
- calculate: 덧셈/뺄셈/곱셈/나눗셈/백분율. 앞 단계의 전체 합계 한 행을 분모로 사용할 수 있음.
- window: 고객·제품별 시간 순서의 직전 값(lag), 누적 합계, 순번, 공동 순위.

예를 들어 ‘오늘 주문액 1위 고객의 최다 주문 품목’은:

1. 고객별 주문액 합계 → 공동 1위 고객 선택.
2. 1단계 customer_id를 참조해 해당 고객들의 오늘 제품별 수량 집계.
3. 고객별 최대 제품 수량 계산.
4. 고객 ID와 최대 수량으로 결과 연결 → 공동 최다 제품 모두 선택.
5. 답변에 필요한 칼럼을 표시.

이 계획은 examples.json과 query-example.json에 포함되어 있습니다.
같은 공통 연산으로 ‘오늘 최다 제품의 구매 고객 순위’, ‘고객 점유율’, ‘두 제품을 주문한 고객’,
‘자기 고객 평균보다 큰 주문’ 등의 다른 질의도 표현할 수 있습니다.
지원된 연산으로 표현할 수 있는 질문 범위에서 범용적이며, 새로운 자료형·외부 데이터·연산은 별도 확장이 필요합니다.

## 질문 예시

- 오늘 제일 주문액이 큰 사람이 가장 많이 주문한 품목은?
- 오늘 가장 많이 주문된 제품을 구매한 고객 중 주문액이 가장 큰 고객은?
- 오늘 주문액 상위 5명과 전체 주문액 대비 점유율은?
- 오늘 주문액이 어제 전체보다 증가한 고객 상위 5명은?
- 오늘 P001과 P002를 모두 주문한 고객은?
- 오늘 고객별 평균 주문액보다 큰 주문들을 보여줘.
- 오늘 주문액 500만원 이상 고객들의 제품별 주문량을 알려줘.
- 최근 1시간 C004의 5분 단위 주문량 증감과 기간 내 누적 주문량을 알려줘.
- 그 고객의 오늘 총 주문액은? (앞 질문에서 고객을 조회한 뒤)

이 목록은 구현 가능한 조회의 예시입니다. 실제 설치된 Gemma의 질문 해석 정확도는 별도로 확인해야 합니다.

## 데이터·시간·정확성

MinIO를 5초마다 확인하고 새 파일 또는 변경 파일만 다운로드합니다.
order_id별 가장 최신 etl_extracted_at을 선택하여 수정본·ETL 재시도 중복을 제거합니다.
한 질문의 계획 생성/검증/실행은 같은 주문 스냅샷과 같은 기준 시각을 사용합니다.
질문 도중 새 주문이 들어오면 다음 질문부터 반영됩니다. 원천 ETL 간격은 기존 10초입니다.

원본과 화면 시간은 코드에서 Asia/Seoul로 고정합니다. 앞으로 한국 시각을 저장하도록 바꾼 시뮬레이터를 전제로 합니다.
기존 UTC 데이터는 변환하지 않고 현재 코드의 한국 시각 규칙으로 해석합니다.
기간이 없으면 오늘, today는 자정부터 질문 기준 시각까지입니다.
yesterday와 사용자 지정 기간의 종료 경계는 제외합니다.
‘어제 전체와 오늘 현재까지’처럼 길이가 다른 기간은 두 구간을 그대로 집계하고 기간 차이를 설명합니다.

공통 원본 칼럼: order_id / customer_id / product_id / product_name / quantity / amount / order_time.
amount는 Parquet total_amount를 매핑한 값입니다. 제품 이름은 앞서 제시된 시뮬레이터의 5개 제품 목록입니다.
고객 이름·주소·재고·이익·실제 원인·미래 예측은 원본에 없습니다.
주문량은 수량 합계, 주문건수는 주문 레코드 수, 주문액은 금액 합계입니다.
평균 주문액은 주문 한 건의 금액 평균이며 고객당 평균이나 단가와 다릅니다.

금액·계산은 Decimal을 사용합니다. 화면 숫자는 소수점 최대 2자리로 표시하고
JSON API에는 실제 계산 문자열을 제공합니다. 모델의 설명 수치와 조건은 실제 표/JSON으로 확인하세요.
계획 검증은 칼럼·자료형·참조·허용 연산을 확인하며 질문의 의미 해석 정확성까지 보증하지 않습니다.

최종 표는 최대 30행, 중간 근거표는 단계당 최대 8행입니다. 다음 단계의 참조에는 표시되지 않은 전체 중간 결과를 사용합니다.
상위 선택 limit은 최대 500이며 keep_ties=true면 동률은 더 남길 수 있습니다.
한 중간 결과는 최대 20,000행입니다. 원본 전체를 먼저 복사하지 않고 원본에서 바로 집계하면 더 많은 주문도 합산할 수 있습니다.
빈 시간 구간을 채우는 연산은 최대 1,000구간, 조회 실행은 30초, 질문 전체는 기본 360초 제한입니다.
데이터 전체를 메모리에 보관하는 실습용 단일 Pod이므로 대규모 데이터에서는 파일 병합·기간별 읽기·별도 분석 엔진을 검토하세요.

빈 고객 선정 결과는 빈 ID 목록으로 전달하여 전체 고객을 잘못 조회하지 않습니다.
고객·제품 동률을 공동 순위로 남길 수 있고, 0으로 나누는 비율과 첫 lag는 null입니다.
SQL/Python 문자열·파일 경로를 실행하지 않습니다. JSON은 정해진 연산과 칼럼으로만 검증·실행합니다.
검증 오류가 생기면 Gemma에 오류를 전달하고 최대 두 번 전체 계획을 고쳐 요청합니다.
계산 후 Gemma 설명이 실패해도 실제 계산표는 표시합니다.

최근 조회 근거는 브라우저가 전달하는 서명된 문맥 토큰으로 유지합니다(최근 2회, 15분).
다른 브라우저의 공용 대화 메모리는 없으며 토큰은 Pod 재시작 후 만료됩니다.
대화 초기화 버튼은 현재 브라우저의 대화와 문맥을 지웁니다.
기존 ETL이 삭제 이벤트를 저장하지 않으므로 DB 삭제는 이 앱이 감지할 수 없습니다.

## 기존 1.x에서 업데이트: 연결 설정 유지

**app.py 한 파일만 교체하지 말고 2.0.1 패키지 전체를 사용하세요.**
새 query_engine.py, planning.py, conversation.py, planner_prompt.txt, examples.json, index.html과 Dockerfile이 필요합니다.
기존에 수정해 둔 YAML과 현재 MinIO/Ollama 설정은 그대로 유지합니다.

GPU 서버의 새 폴더에서 압축을 풀고 실행합니다. 아래 이미지 계정은 실제 계정으로 바꾸세요.

```bash
unzip order-chat.zip
cd order-chat
chmod +x upgrade.sh
./upgrade.sh docker.io/yklee2002/order-chat:2.0.1 default
```

upgrade.sh는 기존 Deployment와 컨테이너 chat을 확인하고, 이미지를 build/push한 뒤
Deployment의 이미지만 변경합니다. 기존 ConfigMap·Secret·NodePort는 유지합니다.
ETL/order-chat이 다른 namespace이면 마지막 인자를 바꾸세요.
Docker Hub 로그인과 docker/kubectl이 구성된 **GPU 서버 Ubuntu 터미널**에서 실행합니다.
이미지를 다시 수정해 빌드할 때는 2.0.2처럼 새로운 태그를 사용하세요.

접속: http://220.149.119.234:30112

```bash
kubectl -n default logs deployment/order-chat --tail=80
kubectl -n default get deployment/order-chat -o jsonpath='{.spec.template.spec.containers[0].image}'
```

Windows PowerShell에서 배포 버전 확인:

```powershell
Invoke-RestMethod 'http://220.149.119.234:30112/healthz'
```

version이 2.0.1인지 확인하고 브라우저를 새로고침하세요.
배포를 하는 것은 사용자 환경의 명령이며 제작 환경에서 실제 서버에 접속하거나 배포하지 않았습니다.

## 신규 설치

order-chat.yaml의 아래 값은 예시입니다. 실제 Service/namespace와 설치 모델 태그에 맞추세요.

```yaml
OLLAMA_URL: "http://ollama.default.svc.cluster.local:11434"
OLLAMA_MODEL: "gemma3:12b"
OLLAMA_TIMEOUT: "180"
OLLAMA_NUM_CTX: "16384"
CHAT_TIMEOUT: "360"
```

```bash
kubectl get svc -A -o wide
kubectl -n default exec deployment/ollama -- ollama list
kubectl -n default get configmap order-etl-config
kubectl -n default get secret order-etl-secret
```

Ollama Deployment 이름이 다르면 명령을 바꾸세요. 모델 태그는 ollama list와 정확히 일치해야 합니다.
새 모델이나 GPU Pod를 만들지 않고 기존 설치된 Ollama/Gemma를 사용합니다.
컨텍스트 길이 16384는 초기 예시이며 GPU 메모리가 부족하면 줄여서 실제 계획 길이와 정확도를 다시 확인하세요.

MinIO ConfigMap 키: MINIO_ENDPOINT / MINIO_BUCKET / MINIO_PREFIX.
Secret 키: MINIO_ACCESS_KEY / MINIO_SECRET_KEY.
모든 참조는 order-chat과 같은 namespace에 있어야 합니다. 기본 prefix는 raw/orders_step1/입니다.
YAML은 기존 자체서명 인증서 실습을 고려하여 MINIO_VERIFY_SSL=false입니다.
S3 API endpoint가 HTTP면 HTTP로 연결하고, HTTPS면 인증서 검증을 제외한 HTTPS 연결을 사용합니다.

이미지와 namespace를 실제 값으로 수정한 뒤:

```bash
docker build -t yklee2002/order-chat:2.0.1 .
docker push yklee2002/order-chat:2.0.1
kubectl apply -f order-chat.yaml
kubectl -n default rollout status deployment/order-chat --timeout=180s
```

30112가 사용 중이면 YAML nodePort를 바꾸세요. 기존 대시보드 포트 30111은 변경하지 않습니다.
화면은 기존 실습처럼 별도 사용자 인증이 없는 NodePort 앱입니다.

## 학생 실습: JSON 계획 직접 실행

Gemma의 해석과 계산 실행을 따로 확인할 수 있습니다.

- GET /api/status: MinIO/Ollama 연결·모델 목록.
- GET /api/catalog: JSON Schema, 공통 연산 설명, 계획 예시 8개.
- POST /api/chat: 자연어 질문 → Gemma 계획 → 실제 계산 → 설명.
- POST /api/query: 학생이 작성한 JSON 계획을 모델 호출 없이 실행.

GPU 서버의 order-chat 폴더에서:

```bash
curl -sS 'http://220.149.119.234:30112/api/query' \
  -H 'Content-Type: application/json' \
  --data-binary @query-example.json
```

Windows PowerShell:

```powershell
$body = Get-Content .\query-example.json -Raw -Encoding UTF8
$r = Invoke-RestMethod -Method Post -Uri 'http://220.149.119.234:30112/api/query' -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
$r.evidence.rows | Format-Table
$r.evidence.steps | Select-Object id,title,row_count
```

학생들은 examples.json의 기간·집계 지표·상위 개수 등을 바꾸며 공통 실행기 결과를 검증할 수 있습니다.
같은 근거표에 대해 Gemma가 질문을 바꾸어 해석하는지 비교하고, 잘못된 칼럼/미래 참조/빈 결과 처리도 확인하세요.

## 파일과 검증

- app.py: Ollama 연결, 계획 검증/교정, API, 동일 스냅샷의 실행 관리.
- query_engine.py: JSON 모델과 범용 관계 연산 실행기 및 칼럼 교정 진단.
- planning.py: 비교 키 보존 보정, 관련 예시 선택, 스키마 축소, 모델 교정 피드백.
- order_data.py: MinIO Parquet 캐시, 최신 주문 중복 제거, 한국 시각 해석.
- conversation.py: 이전 실제 조회 근거를 담은 문맥 토큰.
- planner_prompt.txt / examples.json: 모델에 제공하는 연산 설명과 조합 예시.
- query-example.json: 학생 직접 실행용 JSON 계획.
- index.html / static/fonts: 질문, 최종 표, 단계별 근거, JSON 계획 화면과 Noto Sans KR/OFL.
- Dockerfile / order-chat.yaml / upgrade.sh: 실행 이미지, 신규 배포, 기존 설정을 유지한 업데이트.
- requirements.txt / requirements-dev.txt / tests: 실행 의존성과 검증.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

제작 시 집계·API 및 계획 보정 테스트 68개와 JSON 계획 예시 8개가 통과했습니다.
실제 샘플 Parquet와 모의 Ollama 요청/응답으로 데스크톱·모바일 브라우저 동작도 검증했습니다.
실제 설치된 Gemma의 한국어 계획 생성과 220.149.119.234 서버 연결은 배포 후 검증해야 합니다.

Ollama structured outputs: https://docs.ollama.com/capabilities/structured-outputs
Ollama API chat: https://docs.ollama.com/api/chat
