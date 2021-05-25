
import stripe

from flask import (
    Blueprint,
    current_app,
    jsonify,
    request,
)

stripe_bp = Blueprint('stripe', __name__)  # pylint: disable=invalid-name


@stripe_bp.route('/', methods=['GET'])
def index():
    return 'Begone.', 200


@stripe_bp.route('/price/<string:price_id>', methods=['GET'])
def get_publishable_key(price_id):
    price = stripe.Price.retrieve(price_id)
    return jsonify({
      'publicKey': current_app.config['STRIPE_PUBLISHABLE_KEY'],
      'unitAmount': price['unit_amount'],
      'currency': price['currency']
    })


# Fetch the Checkout Session to display the JSON result on the success page
@stripe_bp.route('/checkout-session/<string:session_id>', methods=['GET'])
def get_checkout_session(session_id):
    checkout_session = stripe.checkout.Session.retrieve(session_id)
    return jsonify(checkout_session)


@stripe_bp.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    
    data = request.get_json()
    if not data or 'items' not in data or not data['items']:
        return jsonify({'error': 'No items were defined'}), 400

    # Stripe uses the key `price` to represent a price ID. Don't get cornfused
    for item in data['items']:
       if 'quantity' not in item or 'price' not in item:
           return jsonify({'error': 'Invalid item data provided'}), 400

    scheme = 'http' if request.host == 'localhost' else 'https'
    domain_url = f'{scheme}://{request.host}'

    success_url = domain_url + '/success?session_id={CHECKOUT_SESSION_ID}'
    cancel_url = domain_url + '/cancel'

    if current_app.config.get('STRIPE_SUCCESS_URL'):
        if current_app.config['STRIPE_SUCCESS_URL'].startswith(scheme):
            success_url = current_app.config['STRIPE_SUCCESS_URL']
        else:
            success_url = domain_url + current_app.config['STRIPE_SUCCESS_URL']

    if current_app.config.get('STRIPE_CANCEL_URL'):
        if current_app.config['STRIPE_CANCEL_URL'].startswith(scheme):
            success_url = current_app.config['STRIPE_CANCEL_URL']
        else:
            success_url = domain_url + current_app.config['STRIPE_CANCEL_URL']

    try:
        # Create new Checkout Session for the order
        # Other optional params include:
        # [billing_address_collection] - to display billing address details on the page
        # [customer] - if you have an existing Stripe Customer ID
        # [payment_intent_data] - lets capture the payment later
        # [customer_email] - lets you prefill the email input in the form
        # For full details see https:#stripe.com/docs/api/checkout/sessions/create

        # ?session_id={CHECKOUT_SESSION_ID} means the redirect will have the session ID set as a query param
        checkout_session = stripe.checkout.Session.create(
            success_url=success_url,
            cancel_url=cancel_url,
            payment_method_types=current_app.config.get("STRIPE_PAYMENT_METHOD_TYPES", "card").split(','),
            mode="payment",
            line_items=data['items'],
        )
        return jsonify({'sessionId': checkout_session['id']}), 201

    except Exception as e:
        return jsonify(error=str(e)), 403
