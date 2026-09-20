"""The zero-configuration boot path.

A fresh install must come up with no ``.env`` at all, which means these three
secrets get generated on first boot and then reused forever. The failure that
matters most is a *second* boot inventing a new CREDENTIAL_ENCRYPTION_KEY:
every credential saved through the settings page would become undecryptable.
Most of what follows is about that.
"""

from __future__ import annotations

import base64
import json
import os
import stat

import pytest

from sre_agent.bootstrap_secrets import (
    MANAGED_SECRETS,
    BootstrapError,
    describe,
    ensure_secrets,
    looks_unset,
    store_path,
)


def test_first_boot_generates_every_secret_and_persists_them(tmp_path):
    path = str(tmp_path / "secrets.json")
    env: dict[str, str] = {}

    report = ensure_secrets(env, path=path)

    assert set(report) == set(MANAGED_SECRETS)
    assert set(report.values()) == {"generated"}
    for name in MANAGED_SECRETS:
        assert env[name].strip()
        assert not looks_unset(env[name])
    assert os.path.exists(path)


def test_a_second_process_reuses_the_stored_values(tmp_path):
    """The whole point: the API, the migrations and the worker must agree."""
    path = str(tmp_path / "secrets.json")
    first: dict[str, str] = {}
    second: dict[str, str] = {}

    ensure_secrets(first, path=path)
    report = ensure_secrets(second, path=path)

    assert set(report.values()) == {"keystore"}
    for name in MANAGED_SECRETS:
        assert second[name] == first[name]


def test_keystore_is_not_world_readable(tmp_path):
    path = str(tmp_path / "secrets.json")
    ensure_secrets({}, path=path)

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"keystore mode {oct(mode)} exposes secrets to other users"


def test_operator_supplied_values_win_and_are_never_written(tmp_path):
    """An externally managed key must not be forked into our own file."""
    path = str(tmp_path / "secrets.json")
    env = {"SECRET_KEY": "an-operator-chose-this-one"}

    report = ensure_secrets(env, path=path)

    assert report["SECRET_KEY"] == "env"
    assert env["SECRET_KEY"] == "an-operator-chose-this-one"
    stored = json.loads(open(path, encoding="utf-8").read())["secrets"]
    assert "SECRET_KEY" not in stored
    # The other two were still needed, so they are present.
    assert "CREDENTIAL_ENCRYPTION_KEY" in stored


def test_the_shipped_env_example_placeholder_is_replaced(tmp_path):
    """Copying .env.example verbatim is the likeliest first run there is.

    Left alone, every install that did so would share one token-signing key.
    """
    path = str(tmp_path / "secrets.json")
    env = {"SECRET_KEY": "sre_platform_dev_key_change_in_production"}

    report = ensure_secrets(env, path=path)

    assert report["SECRET_KEY"] == "generated"
    assert env["SECRET_KEY"] != "sre_platform_dev_key_change_in_production"


@pytest.mark.parametrize(
    "value",
    ["", "   ", None, "YOUR_KEY", "changeme", "sre_platform_dev_key_change_in_production"],
)
def test_placeholders_and_blanks_count_as_unset(value):
    assert looks_unset(value) is True


@pytest.mark.parametrize("value", ["sk-ant-real", "a" * 40, "0"])
def test_real_values_do_not_count_as_unset(value):
    assert looks_unset(value) is False


def test_generated_encryption_key_actually_works_with_the_credential_store(tmp_path):
    """A key of the wrong shape would only fail later, on the first save."""
    from backend import crypto

    path = str(tmp_path / "secrets.json")
    env: dict[str, str] = {}
    ensure_secrets(env, path=path)

    raw = base64.urlsafe_b64decode(env["CREDENTIAL_ENCRYPTION_KEY"].encode("ascii"))
    assert len(raw) == 32, "AES-256-GCM needs exactly 32 bytes"

    previous = {k: os.environ.get(k) for k in ("CREDENTIAL_ENCRYPTION_KEY",)}
    os.environ["CREDENTIAL_ENCRYPTION_KEY"] = env["CREDENTIAL_ENCRYPTION_KEY"]
    try:
        ciphertext = crypto.encrypt_value("xoxb-a-slack-token")
        assert ciphertext is not None and ciphertext.startswith("enc:v")
        assert crypto.decrypt_value(ciphertext) == "xoxb-a-slack-token"
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_a_corrupt_keystore_fails_loudly_instead_of_regenerating(tmp_path):
    """Regenerating would orphan every credential already in the database."""
    path = tmp_path / "secrets.json"
    path.write_text("{ this is not json", encoding="utf-8")

    with pytest.raises(BootstrapError) as excinfo:
        ensure_secrets({}, path=str(path))

    assert "Refusing to regenerate" in str(excinfo.value)


def test_a_keystore_of_the_wrong_shape_also_fails_loudly(tmp_path):
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"secrets": "not-a-mapping"}), encoding="utf-8")

    with pytest.raises(BootstrapError):
        ensure_secrets({}, path=str(path))


def test_unwritable_location_degrades_to_ephemeral_by_default(tmp_path, capsys):
    unwritable = tmp_path / "locked"
    unwritable.mkdir(mode=0o500)
    path = str(unwritable / "nested" / "secrets.json")

    env: dict[str, str] = {}
    try:
        report = ensure_secrets(env, path=path)
    except BootstrapError:  # pragma: no cover - only when running as root
        pytest.skip("running as root: an unwritable directory is still writable")

    assert set(report.values()) == {"ephemeral"}
    assert all(env[name] for name in MANAGED_SECRETS)
    assert "not writable" in capsys.readouterr().err


def test_unwritable_location_is_fatal_when_ephemeral_is_refused(tmp_path):
    unwritable = tmp_path / "locked"
    unwritable.mkdir(mode=0o500)
    path = str(unwritable / "nested" / "secrets.json")

    try:
        with pytest.raises(BootstrapError):
            ensure_secrets({}, path=path, allow_ephemeral=False)
    except pytest.fail.Exception:  # pragma: no cover - only when running as root
        pytest.skip("running as root: an unwritable directory is still writable")


def test_a_partially_populated_keystore_is_topped_up_not_replaced(tmp_path):
    """Upgrading an install that predates one of the managed secrets."""
    path = tmp_path / "secrets.json"
    path.write_text(
        json.dumps({"version": 1, "secrets": {"SECRET_KEY": "a-preexisting-secret"}}),
        encoding="utf-8",
    )

    env: dict[str, str] = {}
    report = ensure_secrets(env, path=str(path))

    assert report["SECRET_KEY"] == "keystore"
    assert env["SECRET_KEY"] == "a-preexisting-secret"
    assert report["CREDENTIAL_ENCRYPTION_KEY"] == "generated"
    stored = json.loads(path.read_text(encoding="utf-8"))["secrets"]
    assert stored["SECRET_KEY"] == "a-preexisting-secret"


def test_store_path_prefers_the_env_override():
    assert store_path({"SENTINEL_SECRET_STORE": "/tmp/x.json"}) == "/tmp/x.json"
    assert store_path({"SENTINEL_SECRET_STORE": "  "}).endswith("secrets.json")


def test_describe_never_leaks_a_value(tmp_path):
    path = str(tmp_path / "secrets.json")
    env: dict[str, str] = {}
    report = ensure_secrets(env, path=path)

    line = describe(report)
    for value in env.values():
        assert value not in line
