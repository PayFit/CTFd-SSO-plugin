from flask import Blueprint, redirect, render_template, request, url_for, session
from wtforms import StringField, BooleanField
from wtforms.validators import InputRequired, Optional

from CTFd.cache import clear_user_session
from CTFd.config import process_boolean_str
from CTFd.forms import BaseForm
from CTFd.forms.fields import SubmitField
from CTFd.models import Users, db
from CTFd.utils import get_app_config
from CTFd.utils import user as current_user
from CTFd.utils.config.visibility import registration_visible
from CTFd.utils.decorators import admins_only
from CTFd.utils.helpers import error_for
from CTFd.utils.logging import log
from CTFd.utils.security.auth import login_user, logout_user

from .models import OAuthClients

import json
import sys
import time
import requests

plugin_bp = Blueprint('sso', __name__, template_folder='templates', static_folder='static', static_url_path='/static/sso')


class OAuthForm(BaseForm):
    name = StringField("Client name", validators=[InputRequired()])
    client_id = StringField("OAuth client id", validators=[InputRequired()])
    client_secret = StringField("OAuth client secret", validators=[InputRequired()])
    access_token_url = StringField("Access token url", validators=[Optional()])
    authorize_url = StringField("Authorization url", validators=[Optional()])
    api_base_url = StringField("User info url", validators=[Optional()])
    server_metadata_url = StringField("Server metadata url", validators=[Optional()])
    color = StringField("Button Color", validators=[InputRequired()])
    enabled = BooleanField("Enabled")
    submit = SubmitField("Add")

def load_bp(oauth):

    @plugin_bp.after_app_request
    def refresh_token(response):
        # We are not using introspection to validate the tokens
        # but we still want to refresh the token, so that the
        # user isn't logged out of the OAuth provider, even though
        # CTFd is used. This is also used to impose the OAuth
        # providers idle time policy

        # If on login page don't refresh the token. Avoid infinite loop
        if request.path.endswith("/login"):
            return response

        if not "token" in session:
            return response
        token = session["token"]
        if not "refresh_token" in token:
            session.pop("token")
            return response
        refresh_token = token["refresh_token"]

        # If expiry of the access_token is longer then 60 seconds
        # away, don't refresh. The refresh_token will expire
        # afterwards in any case
        if time.time() < token["expires_at"] - 60:
            return response

        client_id = session["sso_client_id"]
        if client_id:
            client = OAuthClients.query.filter_by(id=client_id).first()
        else:
            client = OAuthClients.query.filter_by(id=1).first()

        try:
            data = requests.post(client.access_token_url, data = {
                    "refresh_token": refresh_token,
                    "client_id": client.client_id,
                    "client_secret": client.client_secret,
                    "grant_type": "refresh_token",
                    "scope": token["scope"],
                }
            ).json()

            if "error" in data:
                raise ValueError("SSO logout due to idle time")

            if "access_token" in data:
                new_token = data["access_token"]
                session["token"] = str(new_token)
                return response
            else:
                # This shouldn't happen
                raise ValueError("OAuth provider didn't return valid access token")

        except Exception as e:
            if current_user.authed():
                logout_user()
            if "token" in session:
                session.pop("token")
            return response


    @plugin_bp.route('/admin/sso')
    @admins_only
    def sso_list():
        return render_template('list.html')


    @plugin_bp.route('/admin/sso/client/<int:client_id>', methods = ['GET', 'POST', 'DELETE'])
    @admins_only
    def sso_details(client_id):
        client = OAuthClients.query.filter_by(id=client_id).first()
        if request.method == 'DELETE':
            if client:
                client.disconnect(oauth)
                db.session.delete(client)
                db.session.commit()
                db.session.flush()
            return redirect(url_for('sso.sso_list'))
        elif request.method == "POST":
            client.client_id = request.form["client_id"]
            client.client_secret = request.form["client_secret"]
            client.color = request.form["color"]
            client.enabled = ("enabled" in request.form and request.form["enabled"] == "y")
            if request.form["server_metadata_url"]:
                # Get the other URL from the server metadata site
                client.server_metadata_url = request.form["server_metadata_url"]
                metadata = requests.get(client.server_metadata_url).json()
                client.access_token_url = metadata["token_endpoint"]
                client.authorize_url = metadata["authorization_endpoint"]
                client.api_base_url = metadata["issuer"]
            else:
                # Setup default metadata url
                client.access_token_url = request.form["access_token_url"]
                client.authorize_url = request.form["authorize_url"]
                client.api_base_url = request.form["api_base_url"]
                client.server_metadata_url = request.form["api_base_url"] + "/.well-known/openid-configuration"

            db.session.commit()
            db.session.flush()
            client.register(oauth)

            return redirect(url_for('sso.sso_list'))
        else:
          form = OAuthForm()
          form.name.data = client.name
          form.client_id.data = client.client_id
          form.client_secret.data = client.client_secret
          form.access_token_url.data = client.access_token_url
          form.authorize_url.data = client.authorize_url
          form.api_base_url.data = client.api_base_url
          form.server_metadata_url.data = client.server_metadata_url
          form.color.data = client.color
          form.enabled.data = client.enabled
          form.submit.label.text = "Update"

          return render_template('update.html', form=form)


    @plugin_bp.route('/admin/sso/create', methods = ['GET', 'POST'])
    @admins_only
    def sso_create():
        if request.method == "POST":
            name = request.form["name"]
            client_id = request.form["client_id"]
            client_secret = request.form["client_secret"]
            color = request.form["color"]
            enabled = ("enabled" in request.form and request.form["enabled"] == "y")

            if request.form["server_metadata_url"]:
                server_metadata_url = request.form["server_metadata_url"]
                metadata = requests.get(server_metadata_url).json()
                access_token_url = metadata["token_endpoint"]
                authorize_url = metadata["authorization_endpoint"]
                api_base_url = metadata["issuer"]
            else:
                # Setup default metadata url
                access_token_url = request.form["access_token_url"]
                authorize_url = request.form["authorize_url"]
                api_base_url = request.form["api_base_url"]
                server_metadata_url = request.form["api_base_url"] + "/.well-known/openid-configuration"

            client = OAuthClients(
                name=name,
                client_id=client_id,
                client_secret=client_secret,
                access_token_url=access_token_url,
                authorize_url=authorize_url,
                api_base_url=api_base_url,
                server_metadata_url=server_metadata_url,
                color=color,
                enabled=enabled
            )
            db.session.add(client)
            db.session.commit()
            db.session.flush()

            client.register(oauth)

            return redirect(url_for('sso.sso_list'))

        form = OAuthForm()
        return render_template('create.html', form=form)


    @plugin_bp.route("/sso/login/<int:client_id>", methods = ['GET'])
    def sso_oauth(client_id):
        client = oauth.create_client(client_id)
        redirect_uri=url_for('sso.sso_redirect', client_id=client_id, _external=True, _scheme='https')
        return client.authorize_redirect(redirect_uri)


    @plugin_bp.route("/sso/redirect/<int:client_id>", methods = ['GET'])
    def sso_redirect(client_id):
        try:
            client = oauth.create_client(client_id)
            token = client.authorize_access_token()
        except Exception as e:
            log("logins", "[{date}] {ip} - failed sso login attempt")
            error_for(endpoint="auth.login", message=str(e))
            return redirect(url_for("auth.login"))

        try:
            api_data = client.get('').json()
        except:
            api_data = []
        try:
            userinfo = client.parse_id_token(token)
        except:
            userinfo = []

        if "email" in api_data:
            user_email = api_data["email"]
        elif "email" in userinfo :
            user_email = userinfo["email"]
        else:
            user_email = "unknown@example.com"

        if "preferred_username" in api_data:
            user_name = api_data["preferred_username"]
        if "preferred_username" in userinfo:
            user_name = userinfo["preferred_username"]
        elif user_email.find("@") == -1:
            user_name = user_email
        else:
            user_name = user_email[:user_email.find("@")]

        if process_boolean_str(get_app_config("OAUTH_HAS_ROLES")):
            user_roles = api_data.get("roles")
        else:
            user_roles = None;

        user = Users.query.filter_by(email=user_email).first()
        if user is None:
            # Check if we are allowing registration before creating users
            if registration_visible():
                user = Users(
                    name=user_name,
                    email=user_email,
                    verified=True,
                )
                db.session.add(user)
                db.session.commit()
            else:
                log("logins", "[{date}] {ip} - Public registration via SSO blocked")
                error_for(
                    endpoint="auth.login",
                    message="Public registration is disabled. Please try again later.",
                )
                if process_boolean_str(get_app_config("OAUTH_SSO_LOGOUT")):
                    # Use SSO logout function, to allow a new login attempt
                    return redirect(url_for("sso.sso_logout"))
                else:
                    return redirect(url_for("auth.login"))

        user.verified = True
        db.session.commit()

        if process_boolean_str(get_app_config("OAUTH_HAS_ROLES")):
            roles = get_app_config("OAUTH_ALLOWED_ADMIN_ROLES")
            if roles and not user_roles is None and len(user_roles) > 0:
                if type(roles) is str:
                    if "," in roles:
                        allowed_roles = [s for s in roles.split(",")]
                    else:
                        allowed_roles = [roles]
                else:
                    allowed_roles = ["admin"]

                is_admin = False
                for r in user_roles:
                    if r in allowed_roles:
                        is_admin = True
                        break
                if is_admin:
                    user_role = "admin"
                else:
                    user_role = "user"
            else:
                user_role = "user"

            if user_role != user.type:
                user.type = user_role
                db.session.commit()
                user = Users.query.filter_by(email=user_email).first()
                clear_user_session(user_id=user.id)

        login_user(user)
        log("logins", "[{date}] {ip} - {name} logged in via sso", name=user.name)

        session["token"] = token
        if process_boolean_str(get_app_config("OAUTH_SSO_LOGOUT")):
            # Save end_session_endpoint for logout function
            metadata = client.load_server_metadata()
            session["sso_client_id"] = client_id
            session["end_session_endpoint"] = metadata["end_session_endpoint"]

        return redirect(url_for("challenges.listing"))


    @plugin_bp.route("/sso/logout", methods = ['GET'])
    def sso_logout():
        try:
            token = session["token"]
            refresh_token = token["refresh_token"]
            end_session_endpoint = session["end_session_endpoint"]
            client_id = session["sso_client_id"]
            client = OAuthClients.query.filter_by(id=client_id).first()

            retval = requests.post(end_session_endpoint, data = {
                    "refresh_token": refresh_token,
                    "client_id": client.client_id,
                    "client_secret": client.client_secret,
                }
            )
        except Exception as e:
            error_for(endpoint="views.static_html", message="No token or userinfo session data for SSO logout")

        if current_user.authed():
            logout_user()
        return redirect(url_for("views.static_html"))

    return plugin_bp
