# -*- coding: utf-8 -*-

from application.factory import create_app

app = create_app(__name__)
app.config.DEBUG = True
