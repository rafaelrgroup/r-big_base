import hashlib
import hmac
import json
import re
import secrets
import time
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from cryptography.fernet import Fernet, InvalidToken
import pyotp
from fastapi import HTTPException
from .domain import uid, now, timestamp

PERMISSIONS = ['read','enrich','validate','export','admin']
RECENT_TOTP_SECONDS = 300
ROTATION_RECEIPT_SECONDS = 300
def hashed(v): return hashlib.sha256(v.encode()).hexdigest()

def utcnow(): return datetime.now(timezone.utc)

class Limiter:
    """Single-process development bucket; Redis adapter required before multiple workers."""
    def __init__(self): self.buckets={}; self.lock=threading.Lock()
    def consume(self,key,rate=20,burst=40):
        with self.lock:
            t=time.monotonic(); tokens,prev=self.buckets.get(key,(burst,t)); tokens=min(burst,tokens+(t-prev)*rate)
            if tokens<1: self.buckets[key]=(tokens,t); return False
            self.buckets[key]=(tokens-1,t)
            if len(self.buckets)>10000:
                self.buckets={k:v for k,v in self.buckets.items() if t-v[1]<3600}
            return True

class Security:
    def __init__(self,store,root):
        self.store=store; self.hasher=PasswordHasher(time_cost=2,memory_cost=19456,parallelism=1)
        path=Path(root)/'encryption.key'
        if not path.exists():
            with path.open('xb') as f: f.write(Fernet.generate_key())
            path.chmod(0o600)
        self.cipher=Fernet(path.read_bytes()); self.dummy=self.hasher.hash(secrets.token_urlsafe(24))
    def create_user(self,c,username,password,role='user',permissions=None):
        if not isinstance(password,str) or not 12<=len(password)<=256:raise HTTPException(422,'Senha deve ter de 12 a 256 caracteres')
        if not isinstance(username,str):raise HTTPException(422,'Usuário inválido')
        username=username.strip().casefold()
        if not username or len(username)>160: raise HTTPException(422,'Usuário inválido')
        if any(u['username']==username for u in self.store.all(c,'user')): raise HTTPException(409,'Usuário já existe')
        if role not in {'user','admin'}: raise HTTPException(422,'Papel inválido')
        permissions=PERMISSIONS if role=='admin' else (permissions or ['read'])
        if not isinstance(permissions,list) or any(not isinstance(p,str) or p not in PERMISSIONS for p in permissions): raise HTTPException(422,'Permissão inválida')
        u={'id':uid(),'username':username,'password':self.hasher.hash(password),'role':role,'permissions':permissions,'active':True,'otp_secret':self.cipher.encrypt(pyotp.random_base32().encode()).decode(),'otp_enabled':False,'last_otp_step':-1,'created_at':now()}
        self.store.put(c,'user',u); return u
    def revoke_access(self,c,user_id):
        for kind in ['session','challenge','invitation']:
            for item in self.store.all(c,kind):
                if item['user_id']==user_id:c.execute('DELETE FROM objects WHERE kind=? AND id=?',(kind,item['id']))
        for key in self.store.all(c,'api_key'):
            if key['user_id']==user_id:key.update(active=False,revoked_at=now());self.store.put(c,'api_key',key)
        self.erase_rotation_receipts(c,user_id=user_id)

    def invite(self,c,username,permissions=None):
        u=self.create_user(c,username,secrets.token_urlsafe(32),'user',permissions)
        u.update(active=False,activation_pending=True);self.store.put(c,'user',u)
        token=secrets.token_urlsafe(32)
        record={'id':hashed(token),'user_id':u['id'],'expires_at':(datetime.now(timezone.utc)+timedelta(hours=24)).isoformat()}
        self.store.put(c,'invitation',record)
        return {'user':self.public(u),'activation_token':token,'expires_at':record['expires_at']}

    def activate(self,c,token,password):
        invitation=self.store.get(c,'invitation',hashed(token))
        if not invitation or timestamp(invitation['expires_at'])<datetime.now(timezone.utc):raise HTTPException(401,'Convite inválido, utilizado ou expirado')
        u=self.store.get(c,'user',invitation['user_id'])
        if not u or not u.get('activation_pending'):raise HTTPException(401,'Convite indisponível')
        if len(password)<12 or len(password)>256:raise HTTPException(422,'Senha deve ter de 12 a 256 caracteres')
        u.update(password=self.hasher.hash(password),active=True,activation_pending=False)
        self.store.put(c,'user',u);c.execute('DELETE FROM objects WHERE kind=? AND id=?',('invitation',invitation['id']))
        return self.login(c,u['username'],password)

    def public(self,u): return {k:u[k] for k in ['id','username','role','permissions','active','otp_enabled']}
    def secret(self,u): return self.cipher.decrypt(u['otp_secret'].encode()).decode()
    def check_code(self,u,code):
        step=int(time.time())//30
        for n in [step,step-1,step+1]:
            if n>u['last_otp_step'] and hmac.compare_digest(pyotp.TOTP(self.secret(u)).at(n*30),str(code)):
                u['last_otp_step']=n; return True
        return False
    def login(self,c,username,password):
        u=next((u for u in self.store.all(c,'user') if u['username']==username.strip().casefold()),None)
        try: valid=self.hasher.verify(u['password'] if u else self.dummy,password)
        except VerificationError: valid=False
        if not valid or not u or not u['active']: raise HTTPException(401,'Credenciais inválidas')
        token=secrets.token_urlsafe(32)
        self.store.put(c,'challenge',{'id':hashed(token),'user_id':u['id'],'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()})
        result={'challenge':token,'enrollment_required':not u['otp_enabled']}
        if not u['otp_enabled']: result['otp_uri']=pyotp.TOTP(self.secret(u)).provisioning_uri(u['username'],issuer_name='BIG BASE')
        return result
    def complete(self,c,challenge,code):
        ch=self.store.get(c,'challenge',hashed(challenge))
        if not ch or timestamp(ch['expires_at'])<datetime.now(timezone.utc): raise HTTPException(401,'Acesso expirado; entre novamente')
        u=self.store.get(c,'user',ch['user_id'])
        if not u or not u['active'] or not self.check_code(u,code): raise HTTPException(401,'Código inválido ou já utilizado')
        recovery=[]
        if not u['otp_enabled']:
            recovery=[secrets.token_hex(6) for _ in range(10)]; u['recovery_hashes']=[hashed(x) for x in recovery]
        u['otp_enabled']=True; self.store.put(c,'user',u)
        c.execute('DELETE FROM objects WHERE kind=? AND id=?',('challenge',ch['id']))
        token=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(24)
        verified_at=now()
        self.store.put(c,'session',{'id':hashed(token),'user_id':u['id'],'csrf':csrf,'created_at':verified_at,'last_seen':verified_at,'totp_verified_at':verified_at})
        return token,{'user':self.public(u),'csrf':csrf,'recovery_codes':recovery}

    def human_session(self,c,request,u,key):
        """Use after authorize(), within the same transaction and CSRF check."""
        if key is not None or request.headers.get('x-api-key'):
            raise HTTPException(403,{'code':'HUMAN_SESSION_REQUIRED','message':'Esta ação exige uma sessão humana com autenticador.'})
        session=self.store.get(c,'session',hashed(request.cookies.get('bigbase_session','')))
        if not session or session.get('user_id')!=u['id']:
            raise HTTPException(401,{'code':'SESSION_UNAVAILABLE','message':'A sessão não está disponível; entre novamente.'})
        return session

    def require_recent_totp(self,c,request,u,key):
        session=self.human_session(c,request,u,key)
        try:verified=timestamp(session.get('totp_verified_at'))
        except (ValueError,TypeError,OverflowError):verified=None
        current=utcnow()
        if verified is None or not timedelta(0)<=current-verified<=timedelta(seconds=RECENT_TOTP_SECONDS):
            raise HTTPException(403,{'code':'RECENT_TOTP_REQUIRED','message':'Confirme com seu autenticador para continuar esta ação.','max_age_seconds':RECENT_TOTP_SECONDS})
        return session

    def step_up(self,c,request,u,key,code):
        session=self.human_session(c,request,u,key)
        if not self.check_code(u,code):
            raise HTTPException(401,{'code':'TOTP_INVALID_OR_REPLAYED','message':'Código inválido ou já utilizado. Aguarde um novo código do autenticador.'})
        current=utcnow()
        # Persist the user's replay counter and this session's grant atomically.
        # A successful TOTP in another session does not renew this grant.
        session['totp_verified_at']=current.isoformat()
        self.store.put(c,'user',u);self.store.put(c,'session',session)
        return {'confirmed':True,'valid_until':(current+timedelta(seconds=RECENT_TOTP_SECONDS)).isoformat(),'max_age_seconds':RECENT_TOTP_SECONDS}

    def owned_api_key(self,c,user,public_id):
        key=next((x for x in self.store.all(c,'api_key') if x.get('public_id')==public_id),None)
        if not key or (key.get('user_id')!=user['id'] and 'admin' not in user['permissions']):
            raise HTTPException(404,'Chave não encontrada')
        return key

    def public_api_key(self,key):
        fields=['public_id','user_id','name','scopes','sources','created_at','expires_at','rotation_root_id',
                'predecessor_id','successor_id','rotated_at','retire_at','original_expires_at','revoked_at','revoked_by']
        result={field:key[field] for field in fields if field in key}
        result['id']=result.pop('public_id')
        try:unexpired=timestamp(key.get('expires_at'))>utcnow()
        except (ValueError,TypeError):unexpired=False
        active=key.get('active') is True and unexpired
        result.update(active=active,can_rotate=active and not key.get('successor_id'),
                      status='revoked' if key.get('revoked_at') else 'transition' if active and key.get('successor_id') else 'active' if active else 'expired')
        return result

    def erase_rotation_receipts(self,c,*,user_id=None,root_id=None):
        for receipt in self.store.all(c,'key_rotation_receipt'):
            if receipt.get('response_ciphertext') and ((user_id is not None and user_id in {receipt.get('actor_id'),receipt.get('owner_id')})
                                                       or (root_id is not None and receipt.get('rotation_root_id')==root_id)):
                receipt.update(response_ciphertext=None,secret_erased_at=now())
                self.store.put(c,'key_rotation_receipt',receipt)

    def cleanup_rotation_receipts(self):
        current=utcnow()
        with self.store.transaction() as c:
            for receipt in self.store.all(c,'key_rotation_receipt'):
                if not receipt.get('response_ciphertext'):continue
                try:expired=timestamp(receipt.get('expires_at'))<=current
                except (ValueError,TypeError):expired=True
                session=self.store.get(c,'session',receipt['session_id'])
                successor=next((key for key in self.store.all(c,'api_key') if key.get('public_id')==receipt['successor_id']),None)
                if expired or not session or not successor or successor.get('active') is not True:
                    receipt.update(response_ciphertext=None,secret_erased_at=current.isoformat())
                    self.store.put(c,'key_rotation_receipt',receipt)

    def revoke_key_chain(self,c,user,key):
        root=key.get('rotation_root_id',key['public_id']);revoked=[];current=now()
        for member in self.store.all(c,'api_key'):
            if member.get('rotation_root_id',member['public_id'])==root:
                member.update(active=False,revoked_at=member.get('revoked_at',current),revoked_by=user['id'])
                self.store.put(c,'api_key',member);revoked.append(member['public_id'])
        self.erase_rotation_receipts(c,root_id=root)
        return {'ok':True,'rotation_root_id':root,'revoked_ids':sorted(revoked)}

    def rotate_api_key(self,c,request,user,auth_key,public_id,body,idempotency_key):
        session=self.human_session(c,request,user,auth_key)
        parent=self.owned_api_key(c,user,public_id)
        if not isinstance(idempotency_key,str) or not 1<=len(idempotency_key)<=200:
            raise HTTPException(422,{'code':'IDEMPOTENCY_KEY_REQUIRED','message':'Informe Idempotency-Key com até 200 caracteres.'})
        if not isinstance(body,dict) or set(body)-{'otp','grace_seconds'}:
            raise HTTPException(422,'Informe somente otp e grace_seconds')
        grace=body.get('grace_seconds',900)
        if type(grace) is not int or not 0<=grace<=86400:
            raise HTTPException(422,'grace_seconds deve ser inteiro entre 0 e 86400')
        current=utcnow()
        fingerprint=hashed(json.dumps({'key_id':public_id,'grace_seconds':grace},sort_keys=True,separators=(',',':')))
        receipt_id=hashed('rotation:'+session['id']+':'+idempotency_key)
        receipt=self.store.get(c,'key_rotation_receipt',receipt_id)
        if receipt:
            if receipt.get('request_fingerprint')!=fingerprint:
                raise HTTPException(409,{'code':'IDEMPOTENCY_CONFLICT','message':'A mesma operação foi enviada com parâmetros diferentes.'})
            successor=next((key for key in self.store.all(c,'api_key') if key.get('public_id')==receipt['successor_id']),None)
            owner=self.store.get(c,'user',receipt['owner_id'])
            if not owner or not owner['active'] or not owner['otp_enabled'] or not successor or successor.get('revoked_at'):
                raise HTTPException(409,{'code':'ROTATION_REVOKED','message':'A rotação ou o acesso foi revogado.'})
            if (timestamp(receipt['expires_at'])<=current or not receipt.get('response_ciphertext')
                    or successor.get('active') is not True or timestamp(successor['expires_at'])<=current):
                raise HTTPException(409,{'code':'ROTATION_RESPONSE_EXPIRED','message':'A janela de recuperação terminou. Consulte a chave sucessora e faça nova rotação se precisar.',
                                         'successor_id':receipt['successor_id']})
            try:return json.loads(self.cipher.decrypt(receipt['response_ciphertext'].encode())),True
            except (InvalidToken,ValueError,UnicodeError):
                raise HTTPException(409,{'code':'ROTATION_RESPONSE_UNAVAILABLE','message':'Resposta de rotação indisponível. Consulte a chave sucessora.',
                                         'successor_id':receipt['successor_id']})
        owner=self.store.get(c,'user',parent['user_id'])
        if not owner or not owner['active'] or not owner['otp_enabled']:
            raise HTTPException(409,'Titular da chave indisponível')
        if parent.get('successor_id'):
            raise HTTPException(409,{'code':'KEY_ALREADY_ROTATED','message':'Esta chave já foi substituída; selecione sua sucessora.'})
        expires=timestamp(parent['expires_at'])
        if parent.get('active') is not True or expires<=current or parent.get('revoked_at'):
            raise HTTPException(409,{'code':'KEY_NOT_ACTIVE','message':'A chave está revogada ou expirada.'})
        code=body.get('otp')
        if not isinstance(code,str) or not re.fullmatch(r'[0-9]{6}',code):
            raise HTTPException(422,{'code':'NEW_TOTP_REQUIRED','message':'Informe um novo código do autenticador para criar a rotação.'})
        if not self.check_code(user,code):
            raise HTTPException(401,{'code':'TOTP_INVALID_OR_REPLAYED','message':'Código inválido ou já utilizado. Aguarde um novo código do autenticador.'})
        root=parent.get('rotation_root_id',parent['public_id'])
        token='bb_'+secrets.token_urlsafe(32);retire=min(expires,current+timedelta(seconds=grace))
        successor={'id':hashed(token),'public_id':uid(),'user_id':parent['user_id'],'name':parent['name'],
                   'scopes':list(parent['scopes']),'sources':list(parent['sources']),'active':True,'created_at':current.isoformat(),
                   'expires_at':parent['expires_at'],'rotation_root_id':root,'predecessor_id':parent['public_id']}
        parent.update(rotation_root_id=root,successor_id=successor['public_id'],rotated_at=current.isoformat(),
                      original_expires_at=parent.get('original_expires_at',parent['expires_at']),retire_at=retire.isoformat(),
                      expires_at=retire.isoformat(),active=grace>0)
        result={'id':successor['public_id'],'key':token,'expires_at':successor['expires_at'],'predecessor_id':parent['public_id'],
                'predecessor_valid_until':retire.isoformat(),'grace_seconds':grace,'rotation_root_id':root,
                'response_recoverable_until':min(expires,current+timedelta(seconds=ROTATION_RECEIPT_SECONDS)).isoformat()}
        receipt={'id':receipt_id,'session_id':session['id'],'actor_id':user['id'],'owner_id':owner['id'],
                 'successor_id':successor['public_id'],'rotation_root_id':root,'request_fingerprint':fingerprint,
                 'created_at':current.isoformat(),'expires_at':result['response_recoverable_until'],
                 'response_ciphertext':self.cipher.encrypt(json.dumps(result,separators=(',',':')).encode()).decode()}
        self.store.put(c,'api_key',parent);self.store.put(c,'api_key',successor)
        self.store.put(c,'user',user);self.store.put(c,'key_rotation_receipt',receipt)
        self.store.event(c,{'id':uid(),'actor_id':user['id'],'action':'rotate_key','target_id':successor['public_id'],
                           'predecessor_id':parent['public_id'],'rotation_root_id':root,'grace_seconds':grace,
                           'predecessor_valid_until':retire.isoformat(),'expires_at':successor['expires_at'],
                           'request_fingerprint':fingerprint,'at':current.isoformat()})
        return result,False

    def recover(self,c,challenge,code):
        ch=self.store.get(c,'challenge',hashed(challenge))
        if not ch or timestamp(ch['expires_at'])<datetime.now(timezone.utc): raise HTTPException(401,'Acesso expirado')
        u=self.store.get(c,'user',ch['user_id'])
        if not u or not u['active'] or not u['otp_enabled']: raise HTTPException(401,'Recuperação indisponível')
        match=next((x for x in u.get('recovery_hashes',[]) if hmac.compare_digest(x,hashed(code.strip()))),None)
        if not match: raise HTTPException(401,'Código de recuperação inválido ou utilizado')
        u['recovery_hashes']=[];u['otp_enabled']=False;u['last_otp_step']=-1
        u['otp_secret']=self.cipher.encrypt(pyotp.random_base32().encode()).decode()
        self.store.put(c,'user',u)
        for kind in ['session','challenge']:
            for item in self.store.all(c,kind):
                if item['user_id']==u['id']: c.execute('DELETE FROM objects WHERE kind=? AND id=?',(kind,item['id']))
        for key in self.store.all(c,'api_key'):
            if key['user_id']==u['id']: key['active']=False;self.store.put(c,'api_key',key)
        self.erase_rotation_receipts(c,user_id=u['id'])
        token=secrets.token_urlsafe(32)
        self.store.put(c,'challenge',{'id':hashed(token),'user_id':u['id'],'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()})
        return {'challenge':token,'enrollment_required':True,'otp_uri':pyotp.TOTP(self.secret(u)).provisioning_uri(u['username'],issuer_name='BIG BASE')}
    def authorize(self,c,request,scope):
        api_key=request.headers.get('x-api-key'); key=None
        if api_key:
            key=self.store.get(c,'api_key',hashed(api_key))
            if not key or not key['active'] or timestamp(key['expires_at'])<=utcnow(): raise HTTPException(401,'Chave inválida ou expirada')
            u=self.store.get(c,'user',key['user_id'])
        else:
            s=self.store.get(c,'session',hashed(request.cookies.get('bigbase_session','')))
            if not s or timestamp(s['last_seen'])+timedelta(minutes=30)<datetime.now(timezone.utc) or timestamp(s['created_at'])+timedelta(hours=12)<datetime.now(timezone.utc): raise HTTPException(401,'Sessão expirada')
            if request.method not in {'GET','HEAD','OPTIONS'} and not hmac.compare_digest(request.headers.get('x-csrf-token',''),s['csrf']): raise HTTPException(403,'Proteção CSRF: atualize sua sessão')
            u=self.store.get(c,'user',s['user_id']); s['last_seen']=now(); self.store.put(c,'session',s)
        if not u or not u['active'] or not u['otp_enabled']: raise HTTPException(401,'Usuário indisponível')
        if scope not in u['permissions'] or (key and scope not in key['scopes']): raise HTTPException(403,'Permissão insuficiente')
        return u,key
