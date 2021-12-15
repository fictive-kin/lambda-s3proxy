#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
from application import app

#app.run(debug=os.environ.get('DEBUG', False))
app.debug = True
app.run(debug=True)
