import time
from types import SimpleNamespace
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from tollgate.auth import Identity
from tollgate.settings import Settings
from tollgate.storage.database import Database
from tollgate.storage.migrate import upgrade


@pytest.fixture
def identity(tmp_path):
    url='sqlite:///'+str(tmp_path/'identity.sqlite');upgrade(url)
    # The provider adapter verifies JWTs, while test JWKS lookup never uses a network.
    settings=Settings(mode='hosted',database_url='postgresql+psycopg://unused/unused',public_origin='https://app.example',
                      identity_issuer='https://identity.example',identity_audience='tollgate',identity_jwks_url='https://identity.example/jwks')
    verifier=Identity(Database(url),settings)
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    class Keys:
        def get_signing_key_from_jwt(self, token): return SimpleNamespace(key=key.public_key())
    verifier.jwks=Keys()
    yield verifier,key
    verifier.database.dispose()


def token(key,**changes):
    data=dict(sub='actor1',iss='https://identity.example',aud='tollgate',iat=int(time.time())-1,exp=int(time.time())+600)
    data.update(changes)
    return jwt.encode(data,key,algorithm='RS256')


def test_verified_subject(identity):
    verifier,key=identity
    principal=verifier.authenticate(token(key,email='untrusted@example.com',role='owner',workspace_id='forged'))
    assert principal.actor_id=='jwt:actor1' and principal.kind=='human' and principal.workspace_id is None


@pytest.mark.parametrize('changes',[{'exp':0},{'iat':int(time.time())+600},{'aud':'other-resource'},{'iss':'https://attacker.example'},{'sub':''},{'sub':12}])
def test_jwt_required_bindings(identity,changes):
    verifier,key=identity
    with pytest.raises(PermissionError):verifier.authenticate(token(key,**changes))


def test_wrong_signature_and_algorithm(identity):
    verifier,key=identity
    other=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    with pytest.raises(PermissionError):verifier.authenticate(token(other))
    with pytest.raises(PermissionError):verifier.authenticate(jwt.encode({'sub':'actor1','exp':int(time.time())+60},'test-secret-key-at-least-32-characters-long',algorithm='HS256'))
