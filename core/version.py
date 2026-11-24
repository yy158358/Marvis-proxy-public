# -*- coding: utf-8 -*-
"""服务的版本号，唯一来源。

以前这个号写了三份：``serve.py`` 的 ``PROVIDER_VERSION``（0.2.0）、
``api/admin.py`` 的 ``"version"``（0.1.0）、``Handler.server_version``
（``marvis-proxy/0.1``）。``/provider`` 和 ``/admin/health`` 报的号对不上，
查问题时第一件事是先分辨该信哪个 —— 所以收到这里来。

放在 ``core/`` 而不是 ``serve.py``：``serve.py`` 导入 ``api.admin``，反过来 import
会成环。``core`` 是两边共同的下游，不依赖任何一方。
"""

#: 改了行为就动它。语义版本，``/provider``、``/admin/health``、``Server`` 头三处同源。
VERSION = "0.2.1"

#: HTTP ``Server:`` 头里的产品名。用 ``名字/版本`` 的常规形式。
SERVER_ID = "marvis-proxy"
