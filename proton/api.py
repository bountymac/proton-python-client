import base64
import json

import sys
import gnupg
import requests

"""
When using alternative routing, we want to verify as little data as possible. Thus we'll
end up relying mostly on tls key pinning. If we don't disable warnings, a warning will be
constantly popping on the terminal informing the user about it.
https://urllib3.readthedocs.io/en/latest/advanced-usage.html#ssl-warnings
"""
import urllib3

urllib3.disable_warnings()

from concurrent.futures import ThreadPoolExecutor


from .cert_pinning import TLSPinningAdapter
from .constants import (ALT_HASH_DICT, DEFAULT_TIMEOUT, DNS_HOSTS,
                        ENCODED_URLS, SRP_MODULUS_KEY,
                        SRP_MODULUS_KEY_FINGERPRINT)
from .exceptions import (ConnectionTimeOutError, NetworkError,
                         NewConnectionError, ProtonAPIError, TLSPinningError,
                         UnknownConnectionError, MissingDepedencyError)
from .logger import CustomLogger
from .metadata import MetadataBackend
from .srp import User as PmsrpUser


class Session:
    """A Proton Session.

    Provides public key pinning, fetch alternative routes and connect to
    Proton API in a authenticated manner, dump and load sessions.

    All this is possible since it serves as a wrapper for `<Requests>`

    Basic Usage:

      >>> import proton
      >>> s = proton.Session("https://url-to-api.ch")
      >>> s.enable_alternative_routing = True
      >>> s.api_request("/api/endpoint")
      <Response [200]>
    """
    _base_headers = {
        "x-pm-apiversion": "3",
        "Accept": "application/vnd.protonmail.v1+json"
    }
    __force_skip_alternative_routing = False

    @staticmethod
    def load(
        dump, log_dir_path, cache_dir_path,
        tls_pinning=True, timeout=DEFAULT_TIMEOUT,
        proxies=None
    ):
        """Load session from file/keyring.

        This should load the output generated by dump().

        Args:
            log_dir_path (str): path to desired logging directory
            cache_dir_path (str): path to desired cache directory
            tls_pinning (bool): tls pinning
            timeout (tuple|int|float): How long to wait for the server to send
            data before giving up.
            proxies (dict): desired proxies

        Returns:
            proton.Session
        """
        api_url = dump["api_url"]
        appversion = dump["appversion"]
        user_agent = dump["User-Agent"]
        cookies = dump.get("cookies", {})
        s = Session(
            api_url=api_url,
            log_dir_path=log_dir_path,
            cache_dir_path=cache_dir_path,
            appversion=appversion,
            user_agent=user_agent,
            tls_pinning=tls_pinning,
            timeout=timeout,
            proxies=proxies
        )
        requests.utils.add_dict_to_cookiejar(s.s.cookies, cookies)
        s._session_data = dump["session_data"]
        if s.UID is not None:
            s.s.headers["x-pm-uid"] = s.UID
            s.s.headers["Authorization"] = "Bearer " + s.AccessToken
        return s

    def dump(self):
        """Dump session.

        If you want to reuse the session, then dump it and store the values
        somewhere safe.

        Returns:
            dict
        """
        return {
            "api_url": self.__api_url,
            "appversion": self.__appversion,
            "User-Agent": self.__user_agent,
            "cookies": self.s.cookies.get_dict(),
            "session_data": self._session_data
        }

    def __init__(
        self, api_url, log_dir_path, cache_dir_path,
        appversion="Other", user_agent="None",
        tls_pinning=True, ClientSecret=None, timeout=DEFAULT_TIMEOUT,
        proxies=None
    ):
        """Constructs a new Session object.

        Args:
            api_url (string): URL for the new Session object
            appversion (string): version for the new Session object
            user_agent (string): user agent for the new Session` object
            should be in the following syntax:
                - Linux based -> ClientName/client.version (Linux; Distro/distro_version)
                - Non-linux based -> ClientName/client.version (OS)
            tls_pinning (bool): wether tls pinning should be enabled for the new
                Session object.
            ClientSecret (string): secret token for the new Session object that
                is added to the payload with key `ClientSecret`. [OPTIONAL]
            timeout (int|float|tuple): How long to wait for the server to send
                data before giving up. [OPTIONAL]
            proxies (dict): proxies to be used by the new Session object.
                This is mutually exclusive with `tls_pinning`. [OPTIONAL]
        """
        self.__api_url = api_url
        self.__appversion = appversion
        self.__user_agent = user_agent
        self.__clientsecret = ClientSecret
        self.__timeout = timeout
        self.__tls_pinning_enabled = tls_pinning
        self._logger = CustomLogger()
        self._logger.set_log_path(log_dir_path)
        self._logger = self._logger.logger
        self.__metadata = MetadataBackend.get_backend()
        self.__metadata.cache_dir_path = cache_dir_path
        self.__metadata.logger = self._logger
        self.__allow_alternative_routes = None

        # Verify modulus
        self.__gnupg = gnupg.GPG()
        self.__gnupg.import_keys(SRP_MODULUS_KEY)

        self._session_data = {}

        self.s = requests.Session()

        if proxies and self.__tls_pinning_enabled:
            raise RuntimeError("Not allowed to add proxies while TLS Pinning is enabled")

        self.s.proxies = proxies

        if self.__tls_pinning_enabled:
            self.s.mount(self.__api_url, TLSPinningAdapter())

        self.s.headers["x-pm-appversion"] = appversion
        self.s.headers["User-Agent"] = user_agent

    def api_request(
        self, endpoint,
        jsondata=None, additional_headers=None,
        method=None, params=None, _skip_alt_routing_for_api_check=False
    ):
        """Make API request.

        Args:
            endpoint (string): API endpoint.
            jsondata (json): json to send in the body.
            additional_headers (dict): additional (dictionary of) headers to send.
            method (string): get|post|put|delete|patch.
            params (dict|tuple): URL parameters to append to the URL. If a dictionary or
                list of tuples ``[(key, value)]`` is provided, form-encoding will
                take place.
            _skip_alt_routing_for_api_check (bool): used to temporarly skip alt routing.

        Returns:
            requests.Response
        """
        if self.__allow_alternative_routes is None:
            msg = "Alternative routing has not been configured before making API requests. " \
                "Please either enable or disable it before making any requests."
            self._logger.info(msg)
            raise RuntimeError(msg)

        fct = self.s.post

        if method is None:
            if jsondata is None:
                fct = self.s.get
            else:
                fct = self.s.post
        else:
            fct = {
                "get": self.s.get,
                "post": self.s.post,
                "put": self.s.put,
                "delete": self.s.delete,
                "patch": self.s.patch
            }.get(method.lower())

        if fct is None:
            raise ValueError("Unknown method: {}".format(method))

        _url = self.__api_url
        _verify = True

        if not self.__metadata.try_original_url(
            self.__allow_alternative_routes,
            self.__force_skip_alternative_routing
        ):
            _url = self.__metadata.get_alternative_url()
            _verify = False

        request_params = {
            "url": _url,
            "endpoint": endpoint,
            "headers": additional_headers,
            "json": jsondata,
            "timeout": self.__timeout,
            "verify": _verify,
            "params": params
        }

        exception_class = None
        exception_msg = None

        try:
            response = self.__make_request(fct, **request_params)
        except (
            NewConnectionError,
            ConnectionTimeOutError,
            TLSPinningError,
        ) as e:
            self._logger.exception(e)
            exc_type, *_ = sys.exc_info()
            exception_class = exc_type
            exception_msg = e
        except (Exception, requests.exceptions.BaseHTTPError) as e:
            self._logger.exception(e)
            raise UnknownConnectionError(e)

        if exception_class and (not self.__allow_alternative_routes or _skip_alt_routing_for_api_check or self.__force_skip_alternative_routing): # noqa
            self._logger.info("{}: {}".format(exception_class, exception_msg))
            raise exception_class(exception_msg)
        elif (
            exception_class in [NewConnectionError, ConnectionTimeOutError, TLSPinningError]
            and not self._is_api_reacheable()
        ):
            response = self.__try_with_alt_routing(fct, **request_params)

        try:
            status_code = response.status_code
        except: # noqa
            status_code = False

        try:
            json_error = False
            response = response.json()
        except json.decoder.JSONDecodeError as e:
            json_error = e

        if json_error and status_code != 200:
            self._logger.exception(json_error)
            raise ProtonAPIError(
                {
                    "Code": response.status_code,
                    "Error": response.reason,
                    "Headers": response.headers
                }
            )

        # This check is needed for routers or any other clients that will ask for other
        # data that is not provided in json format, such as when asking /vpn/config for
        # a .ovpn template
        try:
            if response["Code"] not in [1000, 1001]:
                if response["Code"] == 9001:
                    self.__captcha_token = response["Details"]["HumanVerificationToken"]
                elif response["Code"] == 12087:
                    del self.human_verification_token

                raise ProtonAPIError(response)
        except TypeError as e:
            if status_code != 200:
                raise TypeError(e)

        return response

    def __try_with_alt_routing(self, fct, **request_params):
        alternative_routes = self.get_alternative_routes_from_dns()

        request_params["verify"] = False
        response = None

        for route in alternative_routes:
            _alt_url = "https://{}".format(route)
            request_params["url"] = _alt_url

            if self.__tls_pinning_enabled:
                self.s.mount(_alt_url, TLSPinningAdapter(ALT_HASH_DICT))

            self._logger.info("Trying {}".format(_alt_url))
            try:
                response = self.__make_request(fct, **request_params)
            except Exception as e: # noqa
                self._logger.exception(e)
                continue
            else:
                self._logger.info("Storing alternative route: {}".format(_alt_url))
                self.__metadata.store_alternative_route(_alt_url)
                break

        if not response:
            self._logger.info("Possible network error, unable to reach API")
            raise NetworkError("Network error")

        return response

    def __make_request(self, fct, **kwargs):
        _endpoint = kwargs.pop("endpoint")
        _url = kwargs["url"]

        kwargs["url"] = _url + _endpoint
        try:
            ret = fct(**kwargs)
        except requests.exceptions.ConnectionError as e:
            raise NewConnectionError(e)
        except requests.exceptions.Timeout as e:
            raise ConnectionTimeOutError(e)
        except TLSPinningError as e:
            raise TLSPinningError(e)
        except (Exception, requests.exceptions.BaseHTTPError) as e:
            raise UnknownConnectionError(e)

        return ret

    def _is_api_reacheable(self):
        try:
            self.api_request("/tests/ping", _skip_alt_routing_for_api_check=True)
        except (NewConnectionError, ConnectionTimeOutError, TLSPinningError) as e:
            self._logger.exception(e)
            return False

        return True

    def verify_modulus(self, armored_modulus):
        # gpg.decrypt verifies the signature too, and returns the parsed data.
        # By using gpg.verify the data is not returned
        verified = self.__gnupg.decrypt(armored_modulus)

        if not (verified.valid and verified.fingerprint.lower() == SRP_MODULUS_KEY_FINGERPRINT):
            raise ValueError("Invalid modulus")

        return base64.b64decode(verified.data.strip())

    def authenticate(self, username, password):
        """Authenticate user against API.

        Args:
            username (string): proton account username
            password (string): proton account password

        Returns:
            dict

        The returning dict contains the Scope of the account. This allows
        to identify if the account is locked, has unpaid invoices, etc.
        """
        self.logout()

        payload = {"Username": username}
        if self.__clientsecret:
            payload["ClientSecret"] = self.__clientsecret

        info_response = self.api_request("/auth/info", payload)

        modulus = self.verify_modulus(info_response["Modulus"])
        server_challenge = base64.b64decode(info_response["ServerEphemeral"])
        salt = base64.b64decode(info_response["Salt"])
        version = info_response["Version"]

        usr = PmsrpUser(password, modulus)
        client_challenge = usr.get_challenge()
        client_proof = usr.process_challenge(salt, server_challenge, version)

        if client_proof is None:
            raise ValueError("Invalid challenge")

        # Send response
        payload = {
            "Username": username,
            "ClientEphemeral": base64.b64encode(client_challenge).decode(
                "utf8"
            ),
            "ClientProof": base64.b64encode(client_proof).decode("utf8"),
            "SRPSession": info_response["SRPSession"],
        }
        if self.__clientsecret:
            payload["ClientSecret"] = self.__clientsecret

        auth_response = self.api_request("/auth", payload)

        if "ServerProof" not in auth_response:
            raise ValueError("Invalid password")

        usr.verify_session(base64.b64decode(auth_response["ServerProof"]))
        if not usr.authenticated():
            raise ValueError("Invalid server proof")

        self._session_data = {
            "UID": auth_response["UID"],
            "AccessToken": auth_response["AccessToken"],
            "RefreshToken": auth_response["RefreshToken"],
            "PasswordMode": auth_response["PasswordMode"],
            "Scope": auth_response["Scope"].split(),
        }

        if self.UID is not None:
            self.s.headers["x-pm-uid"] = self.UID
            self.s.headers["Authorization"] = "Bearer " + self.AccessToken

        return self.Scope

    def provide_2fa(self, code):
        """Provide Two Factor Authentication Code to the API.

        Args:
            code (string): string of ints

        Returns:
            dict

        The returning dict contains the Scope of the account. This allows
        to identify if the account is locked, has unpaid invoices, etc.
        """
        ret = self.api_request("/auth/2fa", {"TwoFactorCode": code})
        self._session_data["Scope"] = ret["Scope"]

        return self.Scope

    def logout(self):
        """Logout from API."""
        if self._session_data:
            self.api_request("/auth", method="DELETE")
            del self.s.headers["Authorization"]
            del self.s.headers["x-pm-uid"]
            self._session_data = {}

    def refresh(self):
        """Refresh tokens.

        Refresh AccessToken with a valid RefreshToken.
        If the RefreshToken is invalid then the user will have to
        re-authenticate.
        """
        refresh_response = self.api_request(
            "/auth/refresh",
            {
                "ResponseType": "token",
                "GrantType": "refresh_token",
                "RefreshToken": self.RefreshToken,
                "RedirectURI": "http://protonmail.ch"
            }
        )
        self._session_data["AccessToken"] = refresh_response["AccessToken"]
        self._session_data["RefreshToken"] = refresh_response["RefreshToken"]
        self.s.headers["Authorization"] = "Bearer " + self.AccessToken

    def get_alternative_routes_from_dns(self, callback=None):
        """Get alternative routes to circumvent firewalls and API restrictions.

        Args:
            callback (func): a callback method to be called.
                Might be usefull for multi-threading. [OPTIONAL]

        This method leverages the power of ThreadPoolExecutor to async
        check if the provided dns hosts can be reached, and if so, collect the
        alternatives routes provided by them.
        The encoded url are done sync because most often one of the two should work,
        as it should provide the data as quick as possible.

        If callback is passed then the method does not return any value, otherwise it
        returns a set().
        """

        try:
            from dns import message
            from dns.rdatatype import TXT
        except ImportError as e:
            self._logger.exception(e)
            raise MissingDepedencyError(
                "Could not find dnspython package. "
                "Please either install the missing package or disable "
                "alternative routing."
            )

        routes = set()

        for encoded_url in ENCODED_URLS:
            dns_query, dns_encoded_data = self.__generate_dns_message(encoded_url)
            dns_hosts_response = []

            host_and_dns = [(host, dns_encoded_data) for host in DNS_HOSTS]

            with ThreadPoolExecutor(max_workers=len(DNS_HOSTS)) as executor:
                dns_hosts_response = list(
                    executor.map(self.__query_for_dns_data, host_and_dns, timeout=20)
                )
                dns_hosts_response = [dns_url for dns_url in dns_hosts_response if dns_url]

            if len(dns_hosts_response) == 0:
                continue

            for response in dns_hosts_response:
                routes = self.__extract_dns_answer(response, dns_query)

            if len(routes) > 0:
                break

        if not callback:
            return routes

        callback(routes)

    def __generate_dns_message(self, encoded_url):
        """Generate DNS message object.

        Args:
            encoded_url (string): encoded url as per documentation

        Returns:
            tuple():
                dns_query (dns.message.Message): output of dns.message.make_query
                base64_dns_message (base64): encode bytes
        """
        from dns import message
        from dns.rdatatype import TXT

        dns_query = message.make_query(encoded_url, TXT)
        dns_wire = dns_query.to_wire()
        base64_dns_message = base64.urlsafe_b64encode(dns_wire).rstrip(b"=")

        return dns_query, base64_dns_message

    def __query_for_dns_data(self, dns_settings):
        """Query DNS host for data.

        Args:
            dns_settings (tuple):
                host_url (str): http/https url
                dns_encoded_data (str): base64 output
                generate by __generate_dns_message()

        This method uses requests.get to query the url
        for dns data.

        Returns:
            bytes: content of the response
        """
        dns_host, dns_encoded_data = dns_settings[0], dns_settings[1]
        try:
            response = requests.get(
                dns_host,
                headers={"accept": "application/dns-message"},
                timeout=(3.05, 16.95),
                params={"dns": dns_encoded_data}
            )

            if response.status_code == 404:
                return
        except Exception as e: # noqa
            return

        return response.content

    def __extract_dns_answer(self, query_content, dns_query):
        """Extract alternative URL from dns message.

        Args:
            query_content (bytes): content of the response
            dns_query (dns.message.Message): output of dns.message.make_query

        Returns:
            set(): alternative routes for API
        """
        from dns import message
        r = message.from_wire(
            query_content,
            keyring=dns_query.keyring,
            request_mac=dns_query.request_mac,
            one_rr_per_rrset=False,
            ignore_trailing=False
        )
        routes = set()
        for route in r.answer:
            routes = set([str(url).strip("\"") for url in route])

        return routes

    @property
    def captcha_url(self):
        return "{}/core/v4/captcha?Token={}".format(
            self.__api_url, self.__captcha_token
        )

    @property
    def enable_alternative_routing(self):
        """Alternative routing getter."""
        return self.__allow_alternative_routes

    @enable_alternative_routing.setter
    def enable_alternative_routing(self, newvalue):
        """Alternative routing setter.

        If you would like to enable/disable alternative routing
        before making any requests, this should be set to the desired
        value.

        Args:
            newvalue (bool)
        """
        if self.__allow_alternative_routes != bool(newvalue):
            self.__allow_alternative_routes = bool(newvalue)

    @property
    def force_skip_alternative_routing(self):
        """Force skip alternative routing getter."""
        return self.__force_skip_alternative_routing

    @force_skip_alternative_routing.setter
    def force_skip_alternative_routing(self, newvalue):
        """Force skip alternative routing setter.

        Alternative routing is normally used when the usual API is not
        reacheable. In certain cases, such as when connected to the VPN,
        the usual API should be reacheable as the connection is tunneled,
        thus there is not need to reach for the alternative routes and the
        usual API is preffered to be used, for security and reliability.

        Args:
            newvalue (bool)
        """
        self.__force_skip_alternative_routing = bool(newvalue)

    @property
    def human_verification_token(self):
        return (
            self.s.headers.get("X-PM-Human-Verification-Token-Type", None),
            self.s.headers.get("X-PM-Human-Verification-Token", None)
        )

    @human_verification_token.setter
    def human_verification_token(self, newtuplevalue):
        """Set human verification token:

        Args:
            newtuplevalue (tuple): (token_type, token_value)
        """
        self.s.headers["X-PM-Human-Verification-Token-Type"] = newtuplevalue[0]
        self.s.headers["X-PM-Human-Verification-Token"] = newtuplevalue[1]

    @human_verification_token.deleter
    def human_verification_token(self):
        # Safest to use .pop() as it will onyl attempt to remove the key by name
        # while del can also remove the whole dict (in case of code/programming error)
        # Thus to prevent this, pop() is used.

        try:
            self.s.headers.pop("X-PM-Human-Verification-Token-Type")
        except (KeyError, IndexError):
            pass

        try:
            self.s.headers.pop("X-PM-Human-Verification-Token")
        except (KeyError, IndexError):
            pass

    @property
    def UID(self):
        return self._session_data.get("UID", None)

    @property
    def AccessToken(self):
        return self._session_data.get("AccessToken", None)

    @property
    def RefreshToken(self):
        return self._session_data.get("RefreshToken", None)

    @property
    def PasswordMode(self):
        return self._session_data.get("PasswordMode", None)

    @property
    def Scope(self):
        return self._session_data.get("Scope", [])
