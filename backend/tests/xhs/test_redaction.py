import json

import pytest

import backend.app.features.xhs.redaction as redaction


@pytest.mark.parametrize(
    ("identifier", "expected_tokens"),
    [
        ("oauthToken", ("oauth", "token")),
        ("OAuthToken", ("oauth", "token")),
        ("OAUTH_TOKEN", ("oauth", "token")),
        ("xOAuthToken", ("x", "oauth", "token")),
        ("XOAuthToken", ("x", "oauth", "token")),
        ("XCSRFToken", ("x", "csrf", "token")),
        ("xCSRFToken", ("x", "csrf", "token")),
        ("XAPIKey", ("x", "api", "key")),
        ("xAPIKey", ("x", "api", "key")),
        ("APIKey", ("api", "key")),
        ("CSRFToken", ("csrf", "token")),
        ("JWTToken", ("jwt", "token")),
        ("URLToken", ("url", "token")),
        ("AccessToken", ("access", "token")),
        ("RefreshToken", ("refresh", "token")),
        ("PersonalAccessToken", ("personal", "access", "token")),
        ("SessionID", ("session", "id")),
        ("WebSession", ("web", "session")),
        ("oauth_token", ("oauth", "token")),
        ("oauth-token", ("oauth", "token")),
        ("oauth token", ("oauth", "token")),
        ("x_oauth_token", ("x", "oauth", "token")),
        ("x-oauth-token", ("x", "oauth", "token")),
        ("x oauth token", ("x", "oauth", "token")),
        ("api_key", ("api", "key")),
        ("api-key", ("api", "key")),
        ("api key", ("api", "key")),
        ("session_id", ("session", "id")),
        ("session-id", ("session", "id")),
        ("session id", ("session", "id")),
    ],
)
def test_identifier_tokenizer_preserves_acronyms_and_explicit_boundaries(
    identifier: str,
    expected_tokens: tuple[str, ...],
) -> None:
    assert redaction._credential_name_tokens(identifier) == expected_tokens


@pytest.mark.parametrize(
    "tokens",
    [
        ("cookie",),
        ("auth",),
        ("authorization",),
        ("csrf",),
        ("xsrf",),
        ("bearer",),
        ("jwt",),
        ("token",),
        ("password",),
        ("secret",),
        ("access", "token"),
        ("refresh", "token"),
        ("api", "token"),
        ("oauth", "token"),
        ("url", "token"),
        ("auth", "token"),
        ("personal", "access", "token"),
        ("cookie", "string"),
        ("client", "secret"),
        ("api", "key"),
        ("api", "secret"),
        ("csrf", "token"),
        ("xsrf", "token"),
        ("bearer", "token"),
        ("jwt", "token"),
        ("session", "id"),
        ("session", "token"),
        ("session", "cookie"),
        ("web", "session"),
        ("x", "oauth", "token"),
        ("x", "csrf", "token"),
        ("x", "api", "key"),
    ],
)
def test_credential_classifier_accepts_only_complete_credential_grammar(
    tokens: tuple[str, ...],
) -> None:
    assert redaction._is_credential_tokens(tokens) is True


@pytest.mark.parametrize(
    "tokens",
    [
        ("access",),
        ("refresh",),
        ("session",),
        ("session", "title"),
        ("secret", "garden"),
        ("token", "count"),
        ("api", "response"),
        ("oauth", "display", "name"),
        ("api", "key", "note"),
        ("oauth", "token", "status"),
        ("title",),
        ("body",),
        ("x", "session", "title"),
    ],
)
def test_credential_classifier_rejects_noncredential_full_token_sequences(
    tokens: tuple[str, ...],
) -> None:
    assert redaction._is_credential_tokens(tokens) is False


@pytest.mark.parametrize(
    "credential_name",
    [
        "OAuthToken",
        "XOAuthToken",
        "XCSRFToken",
        "XAPIKey",
        "APIKey",
        "CSRFToken",
        "JWTToken",
        "URLToken",
        "AccessToken",
        "RefreshToken",
        "PersonalAccessToken",
        "SessionID",
        "WebSession",
        "oauth_token",
        "oauth-token",
        "oauth token",
        "x_oauth_token",
        "x-oauth-token",
        "x oauth token",
        "api_key",
        "api-key",
        "api key",
    ],
)
def test_direct_and_structured_credential_names_share_one_classifier(
    credential_name: str,
) -> None:
    direct_secret = f"direct-{credential_name}-secret-sentinel"
    structured_secret = f"structured-{credential_name}-secret-sentinel"
    redacted = redaction.redact_credentials({
        credential_name: direct_secret,
        "headers": [{"name": credential_name, "value": structured_secret}],
    })

    rendered = json.dumps(redacted, ensure_ascii=False)
    assert direct_secret not in rendered
    assert structured_secret not in rendered


@pytest.mark.parametrize(
    "ordinary_name",
    [
        "access",
        "refresh",
        "session",
        "session title",
        "secret garden",
        "tokenCount",
        "api response",
        "oauth display name",
        "title",
        "body",
    ],
)
def test_direct_and_structured_noncredentials_are_preserved_without_substrings(
    ordinary_name: str,
) -> None:
    direct_value = f"direct-{ordinary_name}-public-sentinel"
    structured_value = f"structured-{ordinary_name}-public-sentinel"
    redacted = redaction.redact_credentials({
        ordinary_name: direct_value,
        "headers": [{"name": ordinary_name, "value": structured_value}],
    })

    rendered = json.dumps(redacted, ensure_ascii=False)
    assert direct_value in rendered
    assert structured_value in rendered
