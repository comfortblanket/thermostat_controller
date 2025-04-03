import base64
import datetime
import http.server
import json
import queue
import requests
import secrets
import threading
import urllib.parse
import webbrowser


def get_expiration_time(expires_in, now=None):
    if now is None:
        now = datetime.datetime.now()
    
    expiration_time = now + datetime.timedelta(seconds=int(expires_in))
    return expiration_time.isoformat()


def seconds_until_expiration(expiration_time, now=None):
    if now is None:
        now = datetime.datetime.now()
    
    time_remaining = expiration_time - now
    return time_remaining.total_seconds()


def create_empty_credentials(fpath):
    creds = {
        "access_token": "",
        "refresh_token": "",
        "expiration_time": "",
    }

    with open(fpath, "w") as fid:
        json.dump(creds, fid, indent=4)


def load_credentials(fpath, check_well_formed=True):
    with open(fpath, "r") as fid:
        creds = json.load(fid)
    
    if check_well_formed:
        assert "access_token" in creds, "Missing access_token in credentials"
        assert "refresh_token" in creds, "Missing refresh_token in credentials"
        assert "expiration_time" in creds, "Missing expiration_time in credentials"

    return creds


def update_credentials(
            fpath, 
            access_token=None, 
            refresh_token=None, 
            expiration_time=None, 
            check_well_formed=True, 
        ):
    creds = load_credentials(fpath, check_well_formed)

    if access_token is not None:
        creds["access_token"] = access_token

    if refresh_token is not None:
        creds["refresh_token"] = refresh_token

    if expiration_time is not None:
        creds["expiration_time"] = expiration_time

    with open(fpath, "w") as fid:
        json.dump(creds, fid, indent=4)


def get_oath2_token(
            client_id, 
            client_secret, 
            authorization_base_url="https://api.honeywellhome.com/oauth2/authorize", 
            token_url="https://api.honeywellhome.com/oauth2/token", 
            redirect_local_port=8080, 
            auth_state=None, 
        ):
    """\
    Get OAuth2 token using the authorization code flow. This function opens a 
    web browser to allow the user to authorize the application. After 
    authorization, it captures the authorization code from the redirect URL 
    and exchanges it for an access token.

    Parameters:
        client_id (str): The client ID of the application.
        client_secret (str): The client secret of the application.
        authorization_base_url (str): The base URL for authorization.
        token_url (str): The URL to exchange the authorization code for a token.
        redirect_local_port (int): The local port to listen for the redirect.
        auth_state (str): A unique state string to prevent CSRF attacks. If None, a random state will be generated.

    Returns:
        dict: The JSON response containing the access token and related data.
    """

    # Generate a random state for CSRF protection if not provided
    if auth_state is None:
        auth_state = secrets.token_urlsafe(16)  # 16 bytes provides a secure, URL-safe string

    # Define the redirect URI where the local server will listen
    redirect_uri = f"http://localhost:{redirect_local_port}"

    # Construct the full authorization URL
    encoded_params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": auth_state,
    })
    auth_url = f"{authorization_base_url}?{encoded_params}"

    # Open the authorization URL in the user's default web browser
    webbrowser.open(auth_url)

    # Create a queue to pass the token from the server thread to the main thread
    token_queue = queue.Queue()

    # Define a custom HTTP handler to process the redirect
    class OAuthHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            """Handle GET requests from the OAuth2 redirect."""

            # Parse the query parameters from the redirect URL
            parsed_path = urllib.parse.urlparse(self.path)
            query_params = urllib.parse.parse_qs(parsed_path.query)
            code = query_params.get("code", [None])[0]
            received_state = query_params.get("state", [None])[0]

            # Verify the state to prevent CSRF attacks and ensure code is present
            if code and received_state == auth_state:

                # Prepare the token exchange request
                data = {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                }
                # Encode client ID and secret for Basic Auth
                auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
                headers = {
                    "Authorization": f"Basic {auth}",
                    "Content-Type": "application/x-www-form-urlencoded",
                }

                # Exchange the code for an access token
                response = requests.post(token_url, data=data, headers=headers)
                if response.status_code == 200:
                    # Success: put the token in the queue and inform the user
                    token_queue.put(response.json())
                    self.send_response(200)
                    self.send_header("Content-type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"You can close this window now.")
                else:
                    # Token exchange failed
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b"Failed to get token.")
            else:
                # Invalid state or missing code
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Invalid request.")

    # Set up the local HTTP server
    server = http.server.HTTPServer(("localhost", redirect_local_port), OAuthHandler)

    # Start the server in a separate thread to avoid blocking
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()

    # Wait for the token to be received
    token = token_queue.get()  # Blocks until the token is available

    # Shut down the server cleanly and wait for the thread to finish
    server.shutdown()
    server_thread.join()

    # Return the token to the caller
    return token


def refresh_access_token(
            client_id, 
            client_secret, 
            refresh_token, 
            token_url="https://api.honeywellhome.com/oauth2/token", 
        ):
    """\
    Refresh OAuth2 token using the refresh token.

    Parameters:
        client_id (str): The client ID of the application.
        client_secret (str): The client secret of the application.
        refresh_token (str): The refresh token to use for refreshing.
        token_url (str): The URL to exchange the refresh token for a new access token (defaults to Honeywell's token URL).

    Returns:
        dict | bool: The JSON response containing the new access token and related data, or False if the request fails.
    """
    # Encode client ID and secret for Basic Authentication
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    # Prepare headers for the POST request
    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    # Prepare data for the refresh token request
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    # Send the POST request to the token endpoint
    response = requests.post(token_url, data=data, headers=headers)

    # Check if the request was successful
    success = (response.status_code == 200)
    return success, response.json()


def get_locations_and_devices(client_id, access_token):
    """\
    Poll the Honeywell API for device information.

    Parameters:
        client_id (str): The client id of the application.
        access_token (str): The OAuth2 token for authentication.

    Returns:
        dict: The JSON response from the API.
    """
    url = "https://api.honeywellhome.com/v2/locations"
    encoded_params = urllib.parse.urlencode({ "apikey": client_id })
    print(encoded_params)

    response = requests.get(
        f"{url}?{encoded_params}",
        headers = { "Authorization": f"Bearer {access_token}" }, 
    )

    success = (response.status_code == 200)
    return success, response.json()


def post_device_settings(
            client_id, 
            access_token, 
            location_id, 
            device_id, 
            settings, 
        ):
    """\
    Update device settings.

    Parameters:
        client_id (str): The client id of the application.
        access_token (str): The OAuth2 token for authentication.
        location_id (str): The location ID.
        device_id (str): The device ID.
        settings (dict): The settings to update.

    Returns:
        dict: The JSON response from the API.
    """
    url = f"https://api.honeywellhome.com/v2/devices/thermostats/{device_id}"
    response = requests.post(
        url,
        json = settings,
        params = {
            "apikey": client_id,
            "locationId": location_id,
        },
        headers = { "Authorization": f"Bearer {access_token}" }, 
    )

    success = (response.status_code == 200)
    return success, response.json()


if __name__ == "__main__":
    
    client_id = "..."
    client_secret = "..."

    # token_response = get_oath2_token(client_id, client_secret)
    # print(f"token_response: {token_response}"")

    # success, response = refresh_access_token(
    #     client_id, 
    #     client_secret, 
    #     "..."
    # )
    # print(f"success: {success}, response: {response}")
