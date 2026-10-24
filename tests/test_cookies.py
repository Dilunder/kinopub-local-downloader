from http.cookiejar import MozillaCookieJar

import pytest

from app.config import config
from app.schemas import CookieHeaderImport, Settings
from app.services.cookies import from_header, normalize_netscape
from app.services.ytdlp import explain_error


def test_header_preserves_values_and_host_only_session_cookies(tmp_path):
    text, count = from_header("Cookie: session=FAKE_VALUE==; preference=ru; blank=", "kino.watch")
    assert count == 3
    file = tmp_path / "cookies.txt"
    file.write_text(text, encoding="utf-8")
    jar = MozillaCookieJar(str(file))
    jar.load(ignore_discard=True, ignore_expires=True)
    cookies = {cookie.name: cookie for cookie in jar}
    assert cookies["session"].value == "FAKE_VALUE=="
    assert cookies["blank"].value == ""
    assert all(cookie.domain == "kino.watch" and cookie.secure and not cookie.domain_specified for cookie in cookies.values())
    assert normalize_netscape(text)[1] == 3


@pytest.mark.parametrize("header", ["", "session=FAKE\r\nX-Header: value", "session=FAKE\tbad", "session=FAKE; HttpOnly", "session=FAKE; Path=/", "session=FAKE; session=OTHER", "no-equals", "имя=значение"])
def test_invalid_header_never_echoes_values(header):
    with pytest.raises(ValueError) as error:
        from_header(header, "kino.watch")
    assert "FAKE" not in str(error.value)
    assert "OTHER" not in str(error.value)


def test_header_domain_is_restricted():
    with pytest.raises(ValueError):
        from_header("session=FAKE", "evil.test")


def test_request_model_hides_cookie_secret_in_repr():
    request = CookieHeaderImport(cookie_header="session=PRIVATE_FAKE_TOKEN")
    assert "PRIVATE_FAKE_TOKEN" not in repr(request)


def test_cookie_header_api_activates_file_and_preserves_settings(client):
    settings = client.get("/api/settings").json()
    settings.update(browser="edge", quality="720")
    assert client.put("/api/settings", json=settings).status_code == 200
    result = client.post("/api/settings/cookies/header", json={"domain": "kino.watch", "cookie_header": "session=PRIVATE_FAKE_TOKEN; language=ru", "user_agent":"Mozilla/5.0 Test Browser"})
    assert result.status_code == 200
    assert result.json()["count"] == 2
    assert "PRIVATE_FAKE_TOKEN" not in result.text
    saved = client.get("/api/settings").json()
    assert saved["auth_source"] == "file" and saved["browser"] == "edge" and saved["quality"] == "720"
    assert saved["user_agent"] == "Mozilla/5.0 Test Browser"
    assert "PRIVATE_FAKE_TOKEN" not in str(saved)
    assert "PRIVATE_FAKE_TOKEN" in (config.data_dir / "cookies.txt").read_text(encoding="utf-8")


def test_header_import_rejects_invalid_data_and_csrf(client):
    body = {"domain": "kino.watch", "cookie_header": "session=PRIVATE_FAKE_TOKEN"}
    assert client.post("/api/settings/cookies/header", json=body, headers={"x-csrf-token":"invalid"}).status_code == 403
    assert client.post("/api/settings/cookies/header", json=body | {"domain":"evil.test"}).status_code == 422
    error = client.post("/api/settings/cookies/header", json=body | {"cookie_header":"session=PRIVATE_FAKE_TOKEN\nother=value"})
    assert error.status_code == 422
    assert "PRIVATE_FAKE_TOKEN" not in error.text


def test_file_upload_also_activates_file_auth(client):
    text = "# Netscape HTTP Cookie File\n.kino.pub\tTRUE\t/\tTRUE\t2147483647\tsession\tFAKE\n"
    result = client.post("/api/settings/cookies", files={"file":("cookies.txt",text)})
    assert result.status_code == 200
    assert client.get("/api/settings").json()["auth_source"] == "file"


def test_edge_lock_error_names_selected_browser():
    error = explain_error("ERROR: Could not copy Chrome cookie database. See https://example.org for more info", Settings(browser="edge"))
    assert error.code == "cookies_locked"
    assert "Microsoft Edge" in str(error)
    assert "Вставить Cookie" in str(error)
    assert "Chrome" not in str(error) and "подписку" not in str(error)


def test_decryption_error_has_import_instructions():
    error = explain_error("ERROR: Failed to decrypt with DPAPI", Settings(browser="edge"))
    assert error.code == "cookies_decryption" and "Вставить Cookie" in str(error)


def test_metadata_exposes_safe_actionable_error_code(client, monkeypatch):
    async def locked(url, settings):
        raise explain_error("ERROR: Could not copy Chrome cookie database", Settings(browser="edge"))
    monkeypatch.setattr("app.services.ytdlp.get_metadata", locked)
    response = client.post("/api/metadata", json={"url":"https://kino.watch/item/view/1"})
    assert response.status_code == 422 and response.json()["code"] == "cookies_locked"


def test_unrelated_error_is_redacted_and_deduplicated():
    line = "ERROR: test https://cdn.test/file?token=PRIVATE_FAKE_TOKEN"
    error = explain_error(line + "\n" + line, Settings())
    assert "PRIVATE_FAKE_TOKEN" not in str(error)
    assert str(error).count("ERROR: test") == 1


def test_user_agent_cli_argument_and_injection_validation():
    from app.services.ytdlp import metadata_args
    from pydantic import ValidationError
    args = metadata_args("https://kino.watch/item/view/1", Settings(user_agent="Mozilla/5.0 Test Browser"))
    assert args[args.index("--user-agent") + 1] == "Mozilla/5.0 Test Browser"
    with pytest.raises(ValidationError):
        Settings(user_agent="Mozilla/5.0\r\nCookie: FAKE")
