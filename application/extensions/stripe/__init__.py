
import os
import stripe

from .views import stripe_bp


REQUIRED_CONFIG_KEYS = [
    'STRIPE_SECRET_KEY',
    'STRIPE_PUBLISHABLE_KEY',
]

def init_app(app, url_prefix='/stripe'):

    for key in REQUIRED_CONFIG_KEYS:
        if not app.config.get(key):
            app.logger.warning('Missing required config key (%s), cannot init Stripe', key)
            return

    stripe.api_version = '2020-08-27'
    stripe.api_key = app.config['STRIPE_SECRET_KEY']

    app.register_blueprint(stripe_bp, url_prefix=url_prefix)
