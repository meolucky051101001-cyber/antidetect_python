from fastapi.routing import APIRoute
from starlette.routing import Match

import web.app as web_app


def _effective_api_routes():
    for route in web_app.app.routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        effective_contexts = getattr(route, "effective_route_contexts", None)
        if effective_contexts is not None:
            for context in effective_contexts():
                if isinstance(context.original_route, APIRoute):
                    yield context.original_route


def test_static_routes_are_not_shadowed():
    routes = list(_effective_api_routes())
    for route in routes:
        if "{" in route.path:
            continue
        for method in route.methods or []:
            scope = {
                "type": "http",
                "path": route.path,
                "method": method,
                "root_path": "",
                "query_string": b"",
                "headers": [],
            }
            first_full = next(
                (
                    candidate
                    for candidate in routes
                    if candidate.matches(scope)[0] == Match.FULL
                ),
                None,
            )
            assert first_full is route, f"{method} {route.path} is shadowed by {first_full}"
