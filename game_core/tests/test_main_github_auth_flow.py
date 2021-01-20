""" Tests for main.py """

import string
import random

from environs import Env
import pytest
import requests

from firebase_admin import auth
from functions_framework import create_app  # type: ignore

from app.firebase_utils import app, db, firestore
from app.models import UserData

env = Env()
FUNCTION_SOURCE = "app/main.py"
WEB_API_KEY = env("WEB_API_KEY")
GH_TEST_TOKEN = env("GH_TEST_TOKEN")


@pytest.fixture(scope="module")
def auth_flow_client():
    """ Test client """
    return create_app("github_auth_flow", FUNCTION_SOURCE, "http").test_client()


@pytest.fixture(scope="module")
def test_auth_user():
    uid = "test_" + "".join([random.choice(string.ascii_letters) for _ in range(10)])

    # create user
    yield auth.create_user(
        uid=uid,
        display_name="test_user",
        email="test_user@example.com",
    )

    # cleanup
    auth.delete_user(uid)
    db.collection("users").document(uid).delete()
    db.collection("system").document("stats").update(
        {"players": firestore.Increment(-1)}
    )


@pytest.fixture(scope="module")
def test_user_token(test_auth_user):
    """ Create a new test user and get a login key """

    # create custom token
    token = auth.create_custom_token(test_auth_user.uid).decode()

    # get ID token - Python Firebase Auth library doesn't have this function *shrug*
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={WEB_API_KEY}"
    res = requests.post(url, json={"token": token, "returnSecureToken": True})
    res.raise_for_status()

    return res.json()["idToken"]


@pytest.fixture
def test_user_data():
    """ Creates test user data """
    return UserData(
        profileImage="",
        name="Yuan Gao",
        handle="meseta",
        id="930832",
        accessToken=GH_TEST_TOKEN,
    ).dict()


# pylint: disable=redefined-outer-name
def test_unauthenticated(auth_flow_client):
    """ Test unauthenticated """
    res = auth_flow_client.post("/")
    assert res.status_code == 403


def test_user_invalid(auth_flow_client, test_user_token):
    """ Test when user is invalid """

    bad_data = {
        "foo": "bar",
        "moo": "cow",
    }
    res = auth_flow_client.post(
        "/", headers={"Authorization": "Bearer " + test_user_token}, json=bad_data
    )
    assert res.status_code == 400


def test_github_invalid(auth_flow_client, test_user_token, test_user_data):
    """ Test when github token is invalid """

    test_user_data["accessToken"] = "foobar"  # make access token bad
    res = auth_flow_client.post(
        "/", headers={"Authorization": "Bearer " + test_user_token}, json=test_user_data
    )
    assert res.status_code == 400


def test_good_flow(auth_flow_client, test_user_token, test_user_data, test_auth_user):
    """ Test a successful flow """

    res = auth_flow_client.post(
        "/", headers={"Authorization": "Bearer " + test_user_token}, json=test_user_data
    )
    assert res.status_code == 200

    # check firestore
    doc = db.collection("users").document(test_auth_user.uid).get()
    assert doc.exists
    assert doc.get("id") == test_user_data["id"]