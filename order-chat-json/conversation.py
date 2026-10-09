"""브라우저별 이전 조회 근거를 서명한 토큰으로 유지. 공용 대화 메모리는 없습니다."""
import base64
import hashlib
import hmac
import json
import secrets
import time


class Conversation:
    def __init__(self):
        self.key = secrets.token_bytes(32)

    def read(self,token):
        if not token:
            return [],None
        try:
            encoded,signature = token.split('.')
            expected = hmac.new(self.key,encoded.encode(),hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature,expected):raise ValueError()
            payload = json.loads(base64.urlsafe_b64decode(encoded))
            if payload['expires'] < time.time():raise ValueError()
            return payload['turns'],None
        except (ValueError,KeyError,TypeError):
            return [],'이전 조회 문맥이 만료되었습니다. 고객·제품·기간을 다시 명시해 주세요.'

    def write(self,turns,question,evidence):
        turn = dict(question=question,as_of=evidence['as_of'],rows=evidence['rows'][:8],
                    steps=[dict(id=s['id'],title=s['title'],rows=s['rows'][:3],notes=s['notes'])
                           for s in evidence['steps'][-4:]])
        encoded = base64.urlsafe_b64encode(json.dumps(
            dict(expires=time.time()+900,turns=(turns+[turn])[-2:]),ensure_ascii=False,separators=(',',':')).encode()).decode()
        if len(encoded)>32000:
            return None
        return encoded+'.'+hmac.new(self.key,encoded.encode(),hashlib.sha256).hexdigest()
