"""LDAP sign-in, against ldap3's in-memory mock directory: no network, no lab VM."""

import ldap3
import pytest

from agent import auth_store, db

PEOPLE = "ou=People,dc=example,dc=local"
ADMINS = "cn=iqcore-admin,ou=Groups,dc=example,dc=local"


@pytest.fixture
def directory(monkeypatch):
    db.init()
    server = ldap3.Server("mock")
    seed = ldap3.Connection(server, client_strategy=ldap3.MOCK_SYNC)
    seed.strategy.add_entry(f"cn=reader,{PEOPLE}", {"userPassword": "svc-pw"})
    seed.strategy.add_entry(f"uid=asha,{PEOPLE}",
                            {"uid": "asha", "userPassword": "asha-pw", "mail": "asha@example.local"})
    seed.strategy.add_entry(f"uid=ravi,{PEOPLE}", {"uid": "ravi", "userPassword": "ravi-pw"})
    seed.strategy.add_entry(f"uid=meera,{PEOPLE}", {"uid": "meera", "userPassword": "dir-pw"})
    seed.strategy.add_entry(ADMINS, {"cn": "iqcore-admin", "member": [f"uid=ravi,{PEOPLE}"]})

    real = ldap3.Connection
    monkeypatch.setattr(ldap3, "Server", lambda *a, **k: server)
    monkeypatch.setattr(ldap3, "Connection",
                        lambda *a, **k: real(*a, client_strategy=ldap3.MOCK_SYNC, **k))
    monkeypatch.setenv("LDAP_URL", "ldap://mock")
    monkeypatch.setenv("LDAP_BIND_DN", f"cn=reader,{PEOPLE}")
    monkeypatch.setenv("LDAP_BIND_PASSWORD", "svc-pw")
    monkeypatch.setenv("LDAP_ADMIN_GROUP", ADMINS)


def test_directory_user_signs_in_as_a_plain_user(directory):
    result = auth_store.login("asha", "asha-pw")
    assert result["ok"] and result["role"] == "user"
    assert db.find_user("asha")["email"] == "asha@example.local"
    # Second sign-in reuses the row rather than creating another.
    assert auth_store.login("asha", "asha-pw")["user_id"] == result["user_id"]


def test_wrong_or_empty_password_is_refused(directory):
    assert not auth_store.login("asha", "nope")["ok"]
    # Empty password would be an anonymous bind, which LDAP servers accept.
    assert not auth_store.login("asha", "")["ok"]
    assert not auth_store.login("nobody", "asha-pw")["ok"]


def test_admin_group_member_becomes_admin(directory):
    assert auth_store.login("ravi", "ravi-pw")["role"] == "admin"


def test_local_account_is_not_taken_over_by_a_directory_password(directory):
    assert auth_store.signup("meera", "local-pw", "meera@local.test")["ok"]
    assert not auth_store.login("meera", "dir-pw")["ok"]
    assert auth_store.login("meera", "local-pw")["ok"]


def test_filter_injection_finds_nobody(directory):
    assert not auth_store.login("*", "asha-pw")["ok"]


def test_unreachable_directory_gives_a_readable_error(monkeypatch):
    db.init()
    monkeypatch.setenv("LDAP_URL", "ldap://127.0.0.1:1")
    result = auth_store.login("someone-new", "pw")
    assert not result["ok"] and "directory could not be reached" in result["error"]
