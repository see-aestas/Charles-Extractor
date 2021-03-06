from typing import Callable, List, Dict, Tuple, Set
from collections import Counter
import base64
import json
from json import JSONDecodeError
import re

from text_blueprints import *


class CharlesSessionHacker:
    def __init__(self, path_to_session: str, request_transformer: Callable[[bytes], str] = None,
                 response_transformer: Callable[[bytes], bytes] = None):
        with open(path_to_session, "r") as session_file:
            session_data = session_file.read()
        try:
            self.charles_session: dict = json.loads(session_data)
        except Exception as e:
            print("Session could not be decoded. Faulty format (not json)?")

        self.request_transformer = request_transformer if request_transformer else lambda x: x
        self.response_transformer = response_transformer if response_transformer else lambda x: x

    """
    Returns the request body in bytes!
    """

    def _get_charles_request_body(self, request_body: dict) -> bytes:
        encode_body = request_body.get("encoded", None)
        if encode_body:
            return base64.b64decode(request_body)
        if type(request_body["text"]) is str:
            request_body["text"] = request_body["text"].encode()
        return request_body["text"]

    def _set_charles_request_body(self, body_type: str, request_dict: dict, new_data: str):
        request_body = request_dict[body_type]["body"]
        request_body.pop("encoded", None)
        request_body["text"] = new_data

    def _apply_transformer(self, transformer, transformer_type: str, mine_type=None):
        for index, charles_json_element in enumerate(self.charles_session):
            if charles_json_element[transformer_type]["sizes"]["body"] == 0:
                continue

            if mine_type:
                charles_json_element[transformer_type]["mimeType"] = mine_type
                for h_index, header in enumerate(charles_json_element[transformer_type]["header"]["headers"]):
                    if header["name"] == "Content-Type":
                        charles_json_element[transformer_type]["header"]["headers"][h_index]["value"] = mine_type

            request_bytes = self._get_charles_request_body(charles_json_element[transformer_type]["body"])
            new_request_body = transformer(request_bytes)

            for h_index, header in enumerate(charles_json_element[transformer_type]["header"]["headers"]):
                if header["name"] == "Content-Length":
                    charles_json_element[transformer_type]["header"]["headers"][h_index]["value"] = len(
                        new_request_body)

            charles_json_element[transformer_type]["sizes"]["body"] = len(new_request_body)
            self._set_charles_request_body(transformer_type, charles_json_element, new_request_body)

    def _get_headers(self) -> Tuple[Set, Set, Set]:
        header_list: List[List[Tuple[str, str]]] = list()
        header_name_list: List[List[str]] = list()
        request_data: List[bytes] = list()
        endpoint_list = list()

        for json_element in self.charles_session:
            endpoint = json_element["request"]["header"]["firstLine"]
            if endpoint in endpoint_list:
                continue
            endpoint_list.append(endpoint)

            headers = []
            header_names = []
            for h_index, header in enumerate(json_element["request"]["header"]["headers"]):
                name = header["name"]
                value = header["value"]
                header_names.append(name)
                headers.append((name, value))
            header_name_list.append(header_names)
            header_list.append(headers)

            # # TODO identify common request data
            # if json_element["request"]["sizes"]["body"] > 0:
            #     request_content = self._get_charles_request_body(json_element["request"]["body"]).decode()
            # else:
            #     request_content = "#Empty request"
            # request_data.append(request_content)

        flatten_header_names = [name for header in header_name_list for name in header]
        c = Counter(flatten_header_names)

        common_headers = set()
        for key, item in c.items():
            if item / len(endpoint_list) > 0.5:  # Identifiy most common headers
                common_headers.add(key)

        static_headers = set(header_list[0])
        for header in header_list[1:]:
            static_headers = static_headers & set(header)

        all_headers = set(c.keys())
        return common_headers, static_headers, all_headers

    class MethodBlueprint:
        def __init__(self):
            self.function_name = None
            self.rest_type = None
            self.endpoint = None
            self.expected_request = None
            self.expected_response = None
            # Which extra headers for this function
            self.extra_headers = []
            self.unused_headers = []

        def __hash__(self):
            return hash(self.function_name)

        def __eq__(self, other):
            if not isinstance(other, type(self)): return NotImplemented
            return self.function_name == other.function_name

    def _get_method_information(self, common_headers):
        method_blueprint_list = list()

        for json_element in self.charles_session:
            method_blueprint = self.MethodBlueprint()

            rest_type = json_element["method"]
            endpoint: str = json_element["path"]
            function_name = rest_type + "_" + endpoint[1:].replace("/", "_")  # Make the function name python conform
            function_name = function_name.replace(".", "")
            method_blueprint.function_name = function_name

            if method_blueprint in method_blueprint_list:
                continue

            method_blueprint.rest_type = rest_type
            method_blueprint.endpoint = endpoint

            if json_element["request"]["sizes"]["body"] == 0:
                method_blueprint.expected_request = None
            else:
                request_body = self._get_charles_request_body(json_element["request"]["body"]).decode()
                try:
                    request_data = json.loads(request_body)
                    request_data = json.dumps(request_data, indent=4)
                except (TypeError, JSONDecodeError):
                    request_data = request_body
                method_blueprint.expected_request = request_data

            if json_element["response"]["sizes"]["body"] == 0:
                method_blueprint.expected_response = None
            else:
                request_body = self._get_charles_request_body(json_element["response"]["body"]).decode()
                try:
                    response_data = json.loads(request_body)
                    response_data = json.dumps(response_data, indent=4)
                except TypeError:
                    response_data = request_body
                method_blueprint.expected_response = response_data

            request_headers = set()
            for header in json_element["request"]["header"]["headers"]:
                name = header["name"]
                request_headers.add(name)

            method_blueprint.extra_headers = request_headers - common_headers
            method_blueprint.unused_headers = common_headers - request_headers
            method_blueprint_list.append(method_blueprint)
        return method_blueprint_list

    """
    Generates the blueprints for the methods.
    Currently only json is supported. Text or xml requests not.
    skip_hints: Comments for expected request and or response, "all" for skip all
    generate_call_sequence: Creates a function that calls all the generated methods
        in the oder that they have in the session
    hardcoded_requests: methods call the api _post, _get .. with hardcoded values 
    """

    def generate_method_blueprint(self, skip_hints="response", generate_call_sequence=False, hardcoded_requests=False):
        common_headers, static_headers, all_headers = self._get_headers()
        method_blueprint_list = self._get_method_information(common_headers)

        headers_print = dict(static_headers)
        for name in common_headers:
            if name not in headers_print.keys():
                headers_print[name] = "TODO_Define"

        # Todo, hacky
        dump_file = open("out.py", "w")
        headers = all_headers_blueprint.format(all_headers=all_headers)
        dump_file.write(headers)

        for method_blueprint in method_blueprint_list:
            request_description = ""
            response_description = ""
            if skip_hints != "request":
                request_description = request_description_blueprint.format(
                    expected_request=method_blueprint.expected_request)
            if skip_hints != "response":
                response_description = response_description_blueprint.format(
                    expected_response=method_blueprint.expected_response)

            if skip_hints != "all":
                method_description = method_description_blueprint.format(request_description=request_description,
                                                                         response_description=response_description)
                dump_file.write(method_description)

            expected_request = method_blueprint.expected_request
            try:
                expected_request = json.loads(expected_request)
                expected_request = json.dumps(expected_request, indent=8)
                expected_request = expected_request.replace("false", "False").replace("true", "True").replace("null", "None")
                expected_request = expected_request[:expected_request.rfind("\n")] + "\n    }" # fix json dumps format at the end
            except:
                if type(expected_request) is str:
                    expected_request = '"' + method_blueprint.expected_request + '"'

            method_definition = method_definition_blueprint.format(function_name=method_blueprint.function_name,
                                                                   payload=expected_request,
                                                                   endpoint_type=method_blueprint.rest_type,
                                                                   endpoint=method_blueprint.endpoint,
                                                                   add_headers=method_blueprint.extra_headers,
                                                                   remove_headers=method_blueprint.unused_headers)

            # replace empty parameters
            if expected_request is None:
                method_definition = method_definition.replace(empty_payload_format, "")
                method_definition = method_definition.replace(empty_payload_param_format, "")
            method_definition = method_definition.replace(empty_add_header_format, "")
            method_definition = method_definition.replace(empty_remove_header_format, "")

            dump_file.write(method_definition)
        dump_file.close()

    def apply_request_transformer(self, mine_type=None):
        self._apply_transformer(self.request_transformer, "request", mine_type)

    def apply_response_transformer(self, mine_type=None):
        self._apply_transformer(self.response_transformer, "response", mine_type)

    def write_changes_to_session_file(self, path_to_session: str):
        data = json.dumps(self.charles_session)
        with open(path_to_session, "w") as session_file:
            session_file.write(data)


if __name__ == '__main__':
    a = CharlesSessionHacker("Test_OtherExport.chlsj")
    a.apply_request_transformer()
    a.apply_response_transformer()
    a.generate_method_blueprint(skip_hints="none")
    # a.write_changes_to_session_file()
