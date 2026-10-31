"""Google sign-in through the real Authlib Starlette client, registered by `create_app` as in
production, against an in-process fake of Google's OpenID provider: its discovery document,
token endpoint and JWKS, served below Authlib's HTTP client so no socket opens (ADR-0005,
#309, #383). Every ID token the client refuses lands on the sign-in page's inline alert."""

import base64
import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.users import UserStatus
from ai_trainer.main import create_app
from ai_trainer.settings import Settings
from ai_trainer.web.auth import SESSION_COOKIE_NAME

PUBLIC_BASE_URL = "http://localhost:8000"
CLIENT_ID = "test-client.apps.googleusercontent.com"
ISSUER = "https://accounts.google.com"
METADATA_URL = f"{ISSUER}/.well-known/openid-configuration"
AUTHORIZE_URL = f"{ISSUER}/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
KEY_ID = "fake-google-key"
FAILED_SIGN_IN = "/sign-in?error=google"


def _rsa_key() -> RSAKey:
    return RSAKey.generate_key(
        2048, parameters={"kid": KEY_ID, "use": "sig", "alg": "RS256"}, private=True
    )


# One key pair for the module: generating RSA keys is the slow part of every test.
GOOGLE_KEY = _rsa_key()
FOREIGN_KEY = _rsa_key()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass
class FakeGoogle:
    """Google's three OpenID URLs. `mint` turns the nonce Authlib sent into the ID token the
    token endpoint returns, so each test decides what it signs."""

    sub: str
    email: str
    mint: Callable[[FakeGoogle, str], str] = field(default=lambda google, nonce: google.sign())
    nonce: str = ""
    served: list[str] = field(default_factory=list)

    def claims(self, **overrides: object) -> dict[str, object]:
        return {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": self.sub,
            "email": self.email,
            "email_verified": True,
            "name": "Ada Google",
            "nonce": self.nonce,
            # Authlib checks `exp` against the wall clock, which no test reads: issued in 2023,
            # expiring in 2100, so the token is valid whenever the suite runs.
            "iat": 1_700_000_000,
            "exp": 4_102_444_800,
            **overrides,
        }

    def sign(self, key: RSAKey = GOOGLE_KEY, **overrides: object) -> str:
        return jwt.encode({"alg": "RS256", "kid": KEY_ID}, self.claims(**overrides), key)

    def forge(self, alg: str, signature: bytes = b"") -> str:
        """A token joserfc won't make: `signature` covers `<header>.<payload>` when given."""
        header = _b64(json.dumps({"alg": alg, "kid": KEY_ID}).encode())
        signing_input = f"{header}.{_b64(json.dumps(self.claims()).encode())}"
        return f"{signing_input}.{_b64(signature)}" if signature else f"{signing_input}."

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url).split("?")[0]
        self.served.append(url)
        if url == METADATA_URL:
            return httpx2.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": AUTHORIZE_URL,
                    "token_endpoint": TOKEN_URL,
                    "jwks_uri": JWKS_URL,
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        if url == JWKS_URL:
            return httpx2.Response(200, json=KeySet([GOOGLE_KEY]).as_dict(private=False))
        if url == TOKEN_URL:
            return httpx2.Response(
                200,
                json={
                    "access_token": "fake-access-token",
                    "token_type": "Bearer",
                    "expires_in": 3599,
                    "scope": "openid email profile",
                    "id_token": self.mint(self, self.nonce),
                },
            )
        return httpx2.Response(404)


@pytest.fixture
async def users() -> AsyncIterator[SqlAlchemyUsersRepository]:
    engine = build_engine(str(Settings().database_url))
    try:
        yield SqlAlchemyUsersRepository(build_session_factory(engine))
    finally:
        await engine.dispose()


@pytest.fixture
async def google(users: SqlAlchemyUsersRepository) -> AsyncIterator[FakeGoogle]:
    """A fresh Google account per test; the callback commits its user through `create_app`'s
    own engine, so it is deleted at teardown, cascading to its sessions."""
    sub = f"google-sign-in-{uuid4()}"
    fake = FakeGoogle(sub=sub, email=f"{sub}@example.com")
    try:
        yield fake
    finally:
        user = await users.get_by_sub(sub)
        if user is not None:
            await users.delete(user.id)


def _sign_in(google: FakeGoogle) -> httpx2.Response:
    """`/auth/login` → Google's consent screen (skipped) → `/auth/callback`, with the state and
    nonce Authlib put in the authorization URL."""
    settings = Settings(
        google_client_id=CLIENT_ID,
        public_base_url=PUBLIC_BASE_URL,
        session_cookie_secure=False,
        admin_emails=[google.email],
    )
    app = create_app(
        settings, google_client_kwargs={"transport": httpx2.MockTransport(google.handle)}
    )
    # Entered, so the lifespan disposes the app's engine when the sign-in is done.
    with TestClient(app, base_url=PUBLIC_BASE_URL) as client:
        login = client.get("/auth/login", follow_redirects=False)
        assert login.status_code == 302, login.text
        authorize = urlsplit(login.headers["location"])
        assert f"{authorize.scheme}://{authorize.netloc}{authorize.path}" == AUTHORIZE_URL
        query = parse_qs(authorize.query)
        google.nonce = query["nonce"][0]
        return client.get(
            "/auth/callback",
            params={"code": "fake-code", "state": query["state"][0]},
            follow_redirects=False,
        )


async def _assert_refused(
    response: httpx2.Response, google: FakeGoogle, users: SqlAlchemyUsersRepository
) -> None:
    assert response.status_code == 303
    assert response.headers["location"] == FAILED_SIGN_IN
    assert SESSION_COOKIE_NAME not in response.cookies
    assert await users.get_by_sub(google.sub) is None
    # Authlib fetches the JWKS only once it holds the ID token, so the refusal came from
    # validating that token, not from a broken exchange the adapter also turns into the alert.
    assert google.served[-2:] == [TOKEN_URL, JWKS_URL]


async def test_google_sign_in_valid_round_trip_signs_in_an_active_user(
    google: FakeGoogle, users: SqlAlchemyUsersRepository
) -> None:
    response = _sign_in(google)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert response.cookies[SESSION_COOKIE_NAME]
    user = await users.get_by_sub(google.sub)
    assert user is not None
    assert user.email == google.email
    assert user.status == UserStatus.ACTIVE


async def test_google_sign_in_with_a_different_nonce_shows_the_alert_and_creates_no_session(
    google: FakeGoogle, users: SqlAlchemyUsersRepository
) -> None:
    google.mint = lambda fake, nonce: fake.sign(nonce=f"{nonce}-replayed")

    await _assert_refused(_sign_in(google), google, users)


async def test_google_sign_in_with_a_wrong_audience_shows_the_alert(
    google: FakeGoogle, users: SqlAlchemyUsersRepository
) -> None:
    google.mint = lambda fake, nonce: fake.sign(aud="another-client.apps.googleusercontent.com")

    await _assert_refused(_sign_in(google), google, users)


async def test_google_sign_in_with_a_wrong_issuer_shows_the_alert(
    google: FakeGoogle, users: SqlAlchemyUsersRepository
) -> None:
    google.mint = lambda fake, nonce: fake.sign(iss="https://accounts.attacker.example")

    await _assert_refused(_sign_in(google), google, users)


async def test_google_sign_in_signed_by_a_key_outside_the_jwks_shows_the_alert(
    google: FakeGoogle, users: SqlAlchemyUsersRepository
) -> None:
    """Same `kid` as Google's key, so only the signature check can tell them apart."""
    google.mint = lambda fake, nonce: fake.sign(key=FOREIGN_KEY)

    await _assert_refused(_sign_in(google), google, users)


async def test_google_sign_in_with_a_tampered_payload_shows_the_alert(
    google: FakeGoogle, users: SqlAlchemyUsersRepository
) -> None:
    """A genuine token whose claims were rewritten after signing, to another account."""

    def tamper(fake: FakeGoogle, nonce: str) -> str:
        header, _payload, signature = fake.sign().split(".")
        forged = fake.claims(email="admin@example.com")
        return f"{header}.{_b64(json.dumps(forged).encode())}.{signature}"

    google.mint = tamper

    await _assert_refused(_sign_in(google), google, users)


async def test_google_sign_in_with_nonce_supported_false_still_checks_the_nonce(
    google: FakeGoogle, users: SqlAlchemyUsersRepository
) -> None:
    """PYSEC-2026-4357: Authlib before 1.9.0 skipped the nonce check when the ID token itself
    said `nonce_supported: false`, so a replayed token signed in."""
    google.mint = lambda fake, nonce: fake.sign(nonce="replayed", nonce_supported=False)

    await _assert_refused(_sign_in(google), google, users)


async def test_google_sign_in_with_an_unsigned_token_shows_the_alert(
    google: FakeGoogle, users: SqlAlchemyUsersRepository
) -> None:
    google.mint = lambda fake, nonce: fake.forge("none")

    await _assert_refused(_sign_in(google), google, users)


async def test_google_sign_in_with_the_public_key_as_an_hmac_secret_shows_the_alert(
    google: FakeGoogle, users: SqlAlchemyUsersRepository
) -> None:
    """Algorithm confusion: HS256 keyed with Google's public key, which anyone can fetch."""
    public_pem = GOOGLE_KEY.as_pem(private=False)

    def confuse(fake: FakeGoogle, nonce: str) -> str:
        unsigned = fake.forge("HS256").rstrip(".")
        signature = hmac.new(public_pem, unsigned.encode(), hashlib.sha256).digest()
        return fake.forge("HS256", signature)

    google.mint = confuse

    await _assert_refused(_sign_in(google), google, users)


async def test_google_sign_in_with_an_expired_token_shows_the_alert(
    google: FakeGoogle, users: SqlAlchemyUsersRepository
) -> None:
    """Expired in 2023, well past Authlib's 120 s leeway whenever the suite runs."""
    google.mint = lambda fake, nonce: fake.sign(iat=1_700_000_000, exp=1_700_003_600)

    await _assert_refused(_sign_in(google), google, users)


async def test_google_sign_in_with_no_nonce_in_the_token_shows_the_alert(
    google: FakeGoogle, users: SqlAlchemyUsersRepository
) -> None:
    def without_nonce(fake: FakeGoogle, nonce: str) -> str:
        claims = fake.claims()
        del claims["nonce"]
        return jwt.encode({"alg": "RS256", "kid": KEY_ID}, claims, GOOGLE_KEY)

    google.mint = without_nonce

    await _assert_refused(_sign_in(google), google, users)
