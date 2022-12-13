S3 Site Proxy
=============

This is a [Flask](https://flask.palletsprojects.com/) mini app, that uses [Zappa](https://github.com/zappa/Zappa) for deployment ease.

It is specifically used with AWS Lambda behind AWS API Gateway, to serve statically built sites saved to an S3 bucket. In it's simplest use, it merely proxies the S3 files out to the world.

There are 3 instances where it does more than that:

- when a `authorizations.json` file is present in the root of the procied bucket,
- when a `redirects.json` file is present in the root of the proxied S3 bucket,
- and when a `11ty-serverless.json` file is present in the root of the proxied S3 bucket.

The `autorizations.json` file has the following format:

```json
{
  "/protected/file": "c29tZS11c2VyOnNvbWUtcGFzcw==",
  "/protected-folder/.*": {
    "username": "some-user",
    "password": "some-pass",
    "realm": "restricted"
  },
  ...
}
```

and will protect the routes specified within the JSON file. The path specification is used as a regex pattern if a `*` is found in it. Otherwise, it is matched exactly as written. The value can be either a base64 encoded token in the form of `<username>:<password>` (as per the `Basic` authentication specifications) or a full dictionary of unencoded values. (`realm` is _not_ required, but can be set for a specific route this way.)

The `redirects.json` file has the following format:

```JSON
{
  "/redirected/file": {
    "status": "301",
    "target": "https://new-domain.tld/path/to/file",
    "trailing-slash": true
  },
  "/temporarily-redirected/file": {
    "status": "302",
    "target": "/new/path/to/file",
    "trailing-slash": false
  },
  ...
}
```

and will provide redirection capabilities for the various routes defined in the JSON file. This allows for somewhat simple definition of any paths that should actually redirect elsewhere. The JSON key is the path to redirect and the `target` is where to send the user. `status` is the HTTP status code to return with the redirect, and `trailing-slash` indicates if the same redirect path with a trailing slash should also be redirected to the the `target`.


The `11ty-serverless.json` file has the following format:

```JSON
{
  "/preview/:id": "arn:aws:lambda:us-east-1:456788029421:function:<project-prefix>-<env>-<serverless-func-name>",
  ...
}
```

and will create routes that proxy to various other AWS Lambdas using the JSON key as the Flask route specifier and the JSON value as the Lambda ARN to call for the request.


Each of these features will take precedence over any files that may exist in the S3 bucket.


Setup
-----

For both development and deployment, these steps need to be taken first.

- Create a Python virtual environment: `python3 -m venv .venv`
- Activate the virtual environment: `. .venv/bin/activate`
- Install the dependencies: `pip3 install -r requirements.txt`
- Source appropriately privileged AWS credentials (i.e.: export `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_DEFAULT_REGION` in your terminal)


Deployment
----------

- Deploy or update a stage: (all stages are configured within `zappa_settings.json`)
    - **ONCE PER STAGE** Deploy: `zappa deploy <stage>`
    - Update: `zappa update <stage>`
- **ONCE PER STAGE** Create a custom domain name within API Gateway (Must be an EDGE setup, not Regional.)
- **ONCE PER STAGE** Add stage mappings for the custom domain to the appropriate API (which Zappa deployed automatically)


Development
-----------

You can also run this locally, you will just need to set the appropriate environment variables, as per what is in `zappa_settings.json` for the stage that you are wishing to try.

- Run: `export FLASK_APP='application.factory:create_app("shell")'`
- Run: `flask run`
- Open your browser to https://localhost:5000/
