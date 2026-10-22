"""OAuthAuth: refresh-token auth (M9) — DEFERRED.

Not built. Blackboard auth ships as the Playwright launcher plus CookieAuth
until Hamilton IT approves a REST integration (three-legged OAuth with PKCE and
the offline scope). When that lands, this module implements the same
AuthProvider protocol and nothing downstream changes.
"""
