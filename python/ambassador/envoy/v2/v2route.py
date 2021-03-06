# Copyright 2018 Datawire. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License

from typing import Any, Dict, List, Set, Union, TYPE_CHECKING
from typing import cast as typecast

from ..common import EnvoyRoute
from ...cache import Cacheable
from ...ir.irhttpmappinggroup import IRHTTPMappingGroup
from ...ir.irbasemapping import IRBaseMapping

from .v2ratelimitaction import V2RateLimitAction

if TYPE_CHECKING:
    from . import V2Config


def regex_matcher(config: 'V2Config', regex: str, key="regex", safe_key=None, re_type=None) -> Dict[str, Any]:
        # If re_type is specified explicitly, do not query its value from config
        if re_type is None:
            re_type = config.ir.ambassador_module.get('regex_type', 'safe').lower()

        config.ir.logger.debug(f"re_type {re_type}")

        # 'safe' is the default. You must explicitly say "unsafe" to get the unsafe
        # regex matcher.
        if re_type != 'unsafe':
            max_size = int(config.ir.ambassador_module.get('regex_max_size', 200))

            if not safe_key:
                safe_key = "safe_" + key

            return {
                safe_key: {
                    "google_re2": {
                        "max_program_size": max_size
                    },
                    "regex": regex
                }
            }
        else:
            return {
                key: regex
            }


def hostglob_matches(glob: str, value: str) -> bool:
    if glob == "*": # special wildcard
        return True
    elif glob.endswith("*"): # prefix match
        if value.startswith(glob[:-1]):
            return True
    elif glob.startswith("*"): # suffix match
        if value.endswith(glob[1:]):
            return True
    else: # exact match
        return value == glob


class V2Route(Cacheable):
    def __init__(self, config: 'V2Config', group: IRHTTPMappingGroup, mapping: IRBaseMapping) -> None:
        super().__init__()

        # Stash SNI and precedence info where we can find it later.
        if group.get('sni'):
            self['_sni'] = {
                'hosts': group['tls_context']['hosts'],
                'secret_info': group['tls_context']['secret_info']
            }

        if group.get('precedence'):
            self['_precedence'] = group['precedence']

        envoy_route = EnvoyRoute(group).envoy_route

        mapping_prefix = mapping.get('prefix', None)
        route_prefix = mapping_prefix if mapping_prefix is not None else group.get('prefix')

        mapping_case_sensitive = mapping.get('case_sensitive', None)
        case_sensitive = mapping_case_sensitive if mapping_case_sensitive is not None else group.get('case_sensitive', True)

        runtime_fraction: Dict[str, Union[dict, str]] = {
            'default_value': {
                'numerator': mapping.get('weight', 100),
                'denominator': 'HUNDRED'
            }
        }

        if len(mapping) > 0:
            runtime_fraction['runtime_key'] = f'routing.traffic_shift.{mapping.cluster.envoy_name}'

        match = {
            'case_sensitive': case_sensitive,
            'runtime_fraction': runtime_fraction
        }

        if envoy_route == 'prefix':
            match['prefix'] = route_prefix
        elif envoy_route == 'path':
            match['path'] = route_prefix
        else:
            # Cheat.
            if config.ir.edge_stack_allowed and (self.get('_precedence', 0) == -1000000):
                # Force the safe_regex engine.
                match.update({
                    "safe_regex": {
                        "google_re2": {
                            "max_program_size": 200,
                        },
                        "regex": route_prefix
                    }
                })
            else:
                match.update(regex_matcher(config, route_prefix))

        headers = self.generate_headers(config, group)
        if len(headers) > 0:
            match['headers'] = headers

        query_parameters = self.generate_query_parameters(config, group)
        if len(query_parameters) > 0:
            match['query_parameters'] = query_parameters

        self['match'] = match

        # `per_filter_config` is used for customization of an Envoy filter
        per_filter_config = {}

        if mapping.get('bypass_auth', False):
            per_filter_config['envoy.ext_authz'] = {'disabled': True}

        if per_filter_config:
            self['per_filter_config'] = per_filter_config

        request_headers_to_add = group.get('add_request_headers', None)
        if request_headers_to_add:
            self['request_headers_to_add'] = self.generate_headers_to_add(request_headers_to_add)

        response_headers_to_add = group.get('add_response_headers', None)
        if response_headers_to_add:
            self['response_headers_to_add'] = self.generate_headers_to_add(response_headers_to_add)

        request_headers_to_remove = group.get('remove_request_headers', None)
        if request_headers_to_remove:
            if type(request_headers_to_remove) != list:
                request_headers_to_remove = [ request_headers_to_remove ]
            self['request_headers_to_remove'] = request_headers_to_remove

        response_headers_to_remove = group.get('remove_response_headers', None)
        if response_headers_to_remove:
            if type(response_headers_to_remove) != list:
                response_headers_to_remove = [ response_headers_to_remove ]
            self['response_headers_to_remove'] = response_headers_to_remove

        host_redirect = group.get('host_redirect', None)

        if host_redirect:
            # We have a host_redirect. Deal with it.
            self['redirect'] = {
                'host_redirect': host_redirect.service
            }

            path_redirect = host_redirect.get('path_redirect', None)

            if path_redirect:
                self['redirect']['path_redirect'] = path_redirect

            return

        route = {
            'priority': group.get('priority'),
            'timeout': "%0.3fs" % (mapping.get('timeout_ms', 3000) / 1000.0),
            'cluster': mapping.cluster.envoy_name
        }

        idle_timeout_ms = mapping.get('idle_timeout_ms', None)

        if idle_timeout_ms is not None:
            route['idle_timeout'] = "%0.3fs" % (idle_timeout_ms / 1000.0)

        regex_rewrite = self.generate_regex_rewrite(config, group)
        if len(regex_rewrite) > 0:
            route['regex_rewrite'] =  regex_rewrite
        elif mapping.get('rewrite', None):
            route['prefix_rewrite'] = mapping['rewrite']

        if 'host_rewrite' in mapping:
            route['host_rewrite'] = mapping['host_rewrite']

        if 'auto_host_rewrite' in mapping:
            route['auto_host_rewrite'] = mapping['auto_host_rewrite']

        hash_policy = self.generate_hash_policy(group)
        if len(hash_policy) > 0:
            route['hash_policy'] = [ hash_policy ]

        cors = None

        if "cors" in group:
            cors = group.cors
        elif "cors" in config.ir.ambassador_module:
            cors = config.ir.ambassador_module.cors

        if cors:
            # Duplicate this IRCORS, then set its group ID correctly.
            cors = cors.dup()
            cors.set_id(group.group_id)

            route['cors'] = cors.as_dict()

        retry_policy = None

        if "retry_policy" in group:
            retry_policy = group.retry_policy.as_dict()
        elif "retry_policy" in config.ir.ambassador_module:
            retry_policy = config.ir.ambassador_module.retry_policy.as_dict()

        if retry_policy:
            route['retry_policy'] = retry_policy

        # Is shadowing enabled?
        shadow = group.get("shadows", None)

        if shadow:
            shadow = shadow[0]

            weight = shadow.get('weight', 100)

            route['request_mirror_policy'] = {
                'cluster': shadow.cluster.envoy_name,
                'runtime_fraction': {
                    'default_value': {
                        'numerator': weight,
                        'denominator': 'HUNDRED'
                    }
                }
            }

        # Is RateLimit a thing?
        rlsvc = config.ir.ratelimit

        if rlsvc:
            # Yup. Build our labels into a set of RateLimitActions (remember that default
            # labels have already been handled, as has translating from v0 'rate_limits' to
            # v1 'labels').

            if "labels" in group:
                # The Envoy RateLimit filter only supports one domain, so grab the configured domain
                # from the RateLimitService and use that to look up the labels we should use.

                rate_limits = []

                for rl in group.labels.get(rlsvc.domain, []):
                    action = V2RateLimitAction(config, rl)

                    if action.valid:
                        rate_limits.append(action.to_dict())

                if rate_limits:
                    route["rate_limits"] = rate_limits

        # Save upgrade configs.
        if group.get('allow_upgrade'):
            route["upgrade_configs"] = [ { 'upgrade_type': proto } for proto in group.get('allow_upgrade', []) ]

        self['route'] = route


    def host_constraints(self, prune_unreachable_routes: bool) -> Set[str]:
        """Return a set of hostglobs that match (a superset of) all
        hostnames that this route can apply to.

        An emtpy set means that this route cannot possibly apply to
        any hostnames.

        This considers SNI information and HeaderMatchers that
        `exact_match` on the `:authority` header.  There are other
        things that could narrow the set down more, but that this
        function doesn't consider (like regex matches on
        `:authority`), leading to it possibly returning a set that is
        too broad.  That's OK for correctness, it just means that
        we'll emit an Envoy config that contains extra work for Envoy.
        """
        ret = set(self.get('_sni', {}).get('hosts', ['*']))

        if prune_unreachable_routes:
            match = self.get("match", {})
            match_headers = match.get("headers", [])
            for header in match_headers:
                if header.get("name") == ":authority" and "exact_match" in header:
                    if any(hostglob_matches(glob, header["exact_match"]) for glob in ret):
                        return set([header["exact_match"]])
                    else:
                        return set()

        return ret


    @classmethod
    def get_route(cls, config: 'V2Config', cache_key: str,
                  irgroup: IRHTTPMappingGroup, mapping: IRBaseMapping) -> 'V2Route':
        route: 'V2Route'

        cached_route = config.cache[cache_key]

        if cached_route is None:
            # Cache miss.
            # config.ir.logger.info(f"V2Route: cache miss for {cache_key}, synthesizing route")
            
            route = V2Route(config, irgroup, mapping)

            # Cheat a bit and force the route's cache_key.
            route.cache_key = cache_key

            config.cache.add(route)
            config.cache.link(irgroup, route)
        else:
            # Cache hit. We know a priori that it's a V2Route, but let's assert that
            # before casting.
            assert(isinstance(cached_route, V2Route))
            route = cached_route

            # config.ir.logger.info(f"V2Route: cache hit for {cache_key}")

        # One way or another, we have a route now.
        return route

    @classmethod
    def generate(cls, config: 'V2Config') -> None:
        config.routes = []

        for irgroup in config.ir.ordered_groups():
            if not isinstance(irgroup, IRHTTPMappingGroup):
                # We only want HTTP mapping groups here.
                continue

            if irgroup.get('host_redirect') is not None and len(irgroup.get('mappings', [])) == 0:
                # This is a host-redirect-only group, which is weird, but can happen. Do we 
                # have a cached route for it?
                key = f"Route-{irgroup.group_id}-hostredirect"

                # Casting an empty dict to an IRBaseMapping may look weird, but in fact IRBaseMapping
                # is (ultimately) a subclass of dict, so it's the cleanest way to pass in a completely
                # empty IRBaseMapping to V2Route().
                #
                # (We could also have written V2Route to allow the mapping to be Optional, but that
                # makes a lot of its constructor much uglier.)
                route = config.save_element('route', irgroup, cls.get_route(config, key, irgroup, typecast(IRBaseMapping, {})))
                config.routes.append(route)

            # Repeat for our real mappings.
            for mapping in irgroup.mappings:
                key = f"Route-{irgroup.group_id}-{mapping.cache_key}"

                route = config.save_element('route', irgroup, cls.get_route(config, key, irgroup, mapping))
                config.routes.append(route)

    @staticmethod
    def generate_headers(config: 'V2Config', mapping_group: IRHTTPMappingGroup) -> List[dict]:
        headers = []

        group_headers = mapping_group.get('headers', [])

        for group_header in group_headers:
            header = { 'name': group_header.get('name') }

            if group_header.get('regex'):
                header.update(regex_matcher(config, group_header.get('value'), key='regex_match'))
            else:
                header['exact_match'] = group_header.get('value')

            headers.append(header)

        return headers

    @staticmethod
    def generate_query_parameters(config: 'V2Config', mapping_group: IRHTTPMappingGroup) -> List[dict]:
        query_parameters = []

        group_query_parameters = mapping_group.get('query_parameters', [])

        for group_query_parameter in group_query_parameters:
            query_parameter = { 'name': group_query_parameter.get('name') }

            if group_query_parameter.get('regex'):
                query_parameter.update({
                    'string_match': regex_matcher(
                        config,
                        group_query_parameter.get('value'),
                        key='regex'
                    )
                })
            else:
                value = group_query_parameter.get('value', None)
                if value is not None:
                    query_parameter.update({
                        'string_match': {
                            'exact': group_query_parameter.get('value')
                        }
                    })
                else:
                    query_parameter.update({
                        'present_match': True
                    })

            query_parameters.append(query_parameter)

        return query_parameters

    @staticmethod
    def generate_hash_policy(mapping_group: IRHTTPMappingGroup) -> dict:
        hash_policy = {}
        load_balancer = mapping_group.get('load_balancer', None)
        if load_balancer is not None:
            lb_policy = load_balancer.get('policy')
            if lb_policy in ['ring_hash', 'maglev']:
                cookie = load_balancer.get('cookie')
                header = load_balancer.get('header')
                source_ip = load_balancer.get('source_ip')

                if cookie is not None:
                    hash_policy['cookie'] = {
                        'name': cookie.get('name')
                    }
                    if 'path' in cookie:
                        hash_policy['cookie']['path'] = cookie['path']
                    if 'ttl' in cookie:
                        hash_policy['cookie']['ttl'] = cookie['ttl']
                elif header is not None:
                    hash_policy['header'] = {
                        'header_name': header
                    }
                elif source_ip is not None:
                    hash_policy['connection_properties'] = {
                        'source_ip': source_ip
                    }

        return hash_policy

    @staticmethod
    def generate_headers_to_add(header_dict: dict) -> List[dict]:
        headers = []
        for k, v in header_dict.items():
                append = True
                if isinstance(v,dict):
                    if 'append' in v:
                        append = bool(v['append'])
                    headers.append({
                        'header': {
                            'key': k,
                            'value': v['value']
                        },
                        'append': append
                    })
                else:
                    headers.append({
                        'header': {
                            'key': k,
                            'value': v
                        },
                        'append': append  # Default append True, for backward compatability
                    })
        return headers

    @staticmethod
    def generate_regex_rewrite(config: 'V2Config', mapping_group: IRHTTPMappingGroup) -> dict:
        regex_rewrite = {}
        group_regex_rewrite = mapping_group.get('regex_rewrite', None)
        if group_regex_rewrite is not None:
            pattern = group_regex_rewrite.get('pattern', None)            
            if (pattern is not None):
                regex_rewrite.update(regex_matcher(config, pattern, key='regex',safe_key='pattern', re_type='safe')) # regex_rewrite should never ever be unsafe
        substitution = group_regex_rewrite.get('substitution', None)
        if (substitution is not None):
            regex_rewrite["substitution"] = substitution
        return regex_rewrite
