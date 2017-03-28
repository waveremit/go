from functools import wraps
import html
import re
from urllib.parse import quote as urlquote

from flask import Flask, abort, redirect, request, Response
import data
import oauth2client.client

BASE_URL = 'https://go.wave.com'
CLIENT_ID = '249856883115-cs97qlkg5ohogb786l67piussauqhr7o.apps.googleusercontent.com'
LOGIN_DOMAIN = 'wave.com'

app = Flask('go')


def get_actual_request_url():
    if (request.is_secure or
            request.headers.get('X-Forwarded-Proto', 'http') == 'https'):
        return re.sub('^http:', 'https:', request.url)
    return request.url


def is_logged_in():
    try:
        claims = oauth2client.client.verify_id_token(
            request.cookies.get('id_token', ''), CLIENT_ID)
        return claims.get('hd') == LOGIN_DOMAIN
    except oauth2client.crypt.AppIdentityError:
        return False


def require_login(handler):
    @wraps(handler)
    def decorated(*args, **kwargs):
        url = get_actual_request_url()

        # Ensure HTTPS, and ensure we're on go.wave.com.
        proper_url = re.sub(r'^https?://go\.(send)?wave\.com\b', BASE_URL, url)
        if proper_url != url:
            return redirect(proper_url, code=301)  # 301 = permanent redirect

        if is_logged_in():
            return handler(*args, **kwargs)

        if '/.login' in request.headers.get('Referer', ''):
            # We just got here from the login page, yet we don't have
            # a valid login.  Maybe the user can fix it using the sign-in
            # and sign-out buttons on the login page, so go back there.
            return redirect(BASE_URL + '/.login')

        # Get the user to log in and then come back here.  (The part after
        # the '#' tells the login page where to redirect to.)
        return redirect(BASE_URL + '/.login#' + url)

    return decorated


@app.before_request
def before_request():
    data.open_db()


@app.teardown_request
def teardown_request(exception):
    data.close_db()


@app.errorhandler(Exception)
def show_exception(e):
    import traceback
    return make_error_response(''.join(
        traceback.format_exception(type(e), e, e.__traceback__)))


@app.route('/.well-known/acme-challenge/<token>')
def acme(token):
    """Proves to Letsencrypt that we own this domain."""
    key = find_acme_key(token)
    if key is None:
        abort(404)
    return key


@app.route('/.login')
def login():
    url = get_actual_request_url()
    if url.startswith('http:'):
        return redirect('https:' + request.url[5:], code=301)

    return make_page_response('Authentication', '''
<script src="https://apis.google.com/js/platform.js" async defer></script>
<meta name="google-signin-client_id" content="%s">
<meta name="google-signin-hosted_domain" content="%s">

<div>
<span id="state">Sign-in is required.</span>
<span id="signout" style="display: none">
    <a href="#" onclick="sign_out()">Sign out.</a></span>
</div>
<div class="g-signin2" data-onsuccess="on_signin"></div>

<script>
    var user;
    var state = document.getElementById('state');
    var signout = document.getElementById('signout');

    function on_signin(signed_in_user) {
        user = signed_in_user;
        var email = user.getBasicProfile().getEmail();
        if (email.endsWith('@%s')) {
            state.innerText = 'You are signed in as ' + email + '.';
            signout.style.display = 'inline';
            document.cookie = 'id_token=' + user.getAuthResponse().id_token;
            var next = (window.location.hash || '').substring(1);
            if (next) window.location = next;
        } else sign_out();
    }

    function sign_out() {
        if (user) user.disconnect();
        document.cookie = 'id_token=';
        state.innerText = 'Sign-in is required.';
        signout.style.display = 'none';
    }
</script>
''' % (CLIENT_ID, LOGIN_DOMAIN, LOGIN_DOMAIN))


@app.route('/')
@require_login
def home():
    """Shows a directory of all existing links."""
    if get_actual_request_url().rstrip('/') != BASE_URL.rstrip('/'):
        return redirect(BASE_URL)

    rows = [format_html('''
<tr>
<td class="name">go.wave.com/<a href="/.edit?name={name_param}">{name}</a>
<td class="count">{count}
<td class="url"><a href="{url}">{url}</a>
</tr>''', name=name, url=url, name_param=urlquote(name), count=count)
            for name, url, count in data.get_all_links()]

    return make_page_response('Where do you want to go/ today?', '''
<div class="tip">Wondering how this works? <a href="#" onclick="document.getElementById('help').style.display='block'">Learn more.</a></div>

<div id="help" class="tip">
    <div>
        Use go.wave.com/ to make a short, memorable link to any URL.
    </div>
    <div>
        For example, see the entry for "go.wave.com/allhands" below?
        That means you can use "go.wave.com/allhands" as a link
        (in Slack, in a Quip, or in your address bar)
        and it will go to the URL shown in that entry.
    </div>
    <div>
        For faster access,
        set up Chrome to let you type "go foo" in the address bar
        instead of "go.wave.com/foo":
        <ol>
            <li>Open <b>Chrome</b> &gt; <b>Preferences...</b>
            <li>Under <b>Search</b>, click <b>Manage search engines...</b>
            <li>Scroll down to "Other search engines" and find the row at the bottom that looks like this:

        <div><img src="/.static/search-engine-empty.png"></div>

            <li>Fill it in like this:

        <div><img src="/.static/search-engine-filled.png"></div>
            <li>Then click <b>Done</b>.  You're all set!
        </ol>
    </div>
</div>

<div>
    <form action="/.edit">
        Add or edit a shortcut:
        go.wave.com/<input name="name" placeholder="shortcut" size=12>
        <input value="edit" type="submit">
    </form>
</div>

<table class="links" cellpadding=0 cellspacing=0>
<tr><th>shortcut</th><th>clicks</th><th>url</th>
%s
</table>
''' % ''.join(rows))


@app.route('/<path:name>')
@require_login
def go(name):
    """Redirects to a link."""
    url = data.get_url(name) or data.get_url(normalize(name))
    # If "foo/bar/baz" is not found, try "foo/bar" and append "/baz";
    # if that's not found, try "foo" and append "/bar/baz".
    suffix = ''
    while not url and '/' in name:
        name, part = name.rsplit('/', 1)
        suffix = '/' + part + suffix
        url = data.get_url(name) or data.get_url(normalize(name))
    if not url:
        return redirect('/.edit?name=' + urlquote(name + suffix))
    if '%s' in url:
        url = url.replace('%s', urlquote(suffix.lstrip('/')))
    else:
        url += suffix
    qs = (request.query_string or '').encode('utf-8')
    if qs:
        url += ('&' if '?' in url else '?') + qs
    data.log('redirect', name, url)
    data.update_count(name)
    return redirect(url)


@app.route('/.edit')
@require_login
def edit():
    """Shows the form for creating or editing a link."""
    name = request.args.get('name', '').lstrip('.')
    url = data.get_url(name)
    if not name:
        return redirect('/')
    if not url:
        if not data.get_url(normalize(name)):
            name = normalize(name)  # default to normalized when creating
    original_name = message = ''
    if url:
        title = 'Edit go.wave.com/' + name
        message = ' is an existing link. You can change it below.'
        original_name = name
    else:
        title = 'Create go.wave.com/' + name
        message = " isn't an existing link. You can create it below."
    return make_page_response(title, format_html('''
<div><a href="/{name_param}">go.wave.com/{name_param}</a> {message}</div>
<form action="/.save" method="post">
<input type="hidden" name="original_name" value="{original_name}">
<table class="form" cellpadding=0 cellspacing=0><tr valign="center">
  <td>go.wave.com/<input name="name" value="{name}" placeholder="shortcut" size=12>
  <td><span class="arrow">\u2192</span>
  <td><input id="url" name="url" value="{url}" placeholder="URL" size=60>
</tr></table>
<div>
  <input type=submit name="save" value="Save"> &nbsp;
  <input type=submit name="delete" value="Delete"
      onclick="return confirm('Really delete this link?')">
</div>
<script>document.getElementById("url").focus()</script>
</form>

<div class="tip">
Fancy tricks:
<ul>
<li>If go.wave.com/foo is defined,
then go.wave.com/foo?a=b
will expand go.wave.com/foo and append "a=b" as a form variable.
<li>If go.wave.com/foo is defined but not go.wave.com/foo/bar,
then go.wave.com/foo/bar will expand go.wave.com/foo and append "/bar".
<li>If go.wave.com/foo is defined to be a URL that contains "%s",
then go.wave.com/foo/bar
will expand go.wave.com/foo and substitute "bar" for "%s".
</ul>
</div>
''', title=title, message=message, url=url or '',
     name=name, name_param=urlquote(name), original_name=original_name))


@app.route('/.save', methods=['POST'])
@require_login
def save():
    """Creates or edits a link in the database."""
    original_name = request.form.get('original_name', '').lstrip('.')
    name = request.form.get('name', '').lstrip('.')
    url = request.form.get('url', '')
    if not name:
        return make_error_response('The shortcut must be made of letters.')
    if not re.match(r'^(http|https)://', url):
        return make_error_response('URLs must start with http:// or https://.')
    if request.form.get('delete'):
        data.delete_link(original_name)
        data.log('delete', original_name, url)
        return redirect('/')
    try:
        if original_name:
            if not data.update_link(original_name, name, url):
                return make_error_response(
                    'Someone else renamed go.wave.com/%s.' % original_name)
            data.log('update', name, url)
        else:
            data.add_link(name, url)
            data.log('create', name, url)
    except data.IntegrityError:
        return make_error_response('go.wave.com/%s already exists.' % name)
    return redirect('/.edit?name=' + urlquote(name))


def normalize(name):
    """Keeps only lowercase letters, digits, and slashes.  We don't require all
    shortcuts to be made only of these characters, but we encourage it by
    normalizing the shortcut name when prepopulating the creation form.

    We do this to give users confidence that they can hear a spoken link and
    just type it in without having to guess whether to use hyphens or
    underscores as word separators.  For special cases, it's still possible
    to make a shortcut name with punctuation by typing it into the name field.
    """
    return re.sub(r'[^a-z0-9/]', '', name.lower())


@app.route('/.static/<path:filename>')
def static_file(filename):
    return app.send_static_file(filename)


def find_acme_key(token):
    import os
    if token == os.environ.get("ACME_TOKEN"):
        return os.environ.get("ACME_KEY")
    for k, v in os.environ.items():
        if v == token and k.startswith("ACME_TOKEN_"):
            n = k.replace("ACME_TOKEN_", "")
            return os.environ.get("ACME_KEY_{}".format(n))


def make_page_response(title, content):
    return Response(format_html('''
<!doctype html>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="/.static/icon.png">
<link rel="stylesheet" href="/.static/style.css">
<title>{title}</title>
<div class="corner">
    <a href="/">Home</a> \xb7 <a href="/.login">Sign out</a>
</div>
<h1>{title}</h1>
''', title=title) + content)


def make_error_response(message):
    """Makes a nice error page."""
    return make_page_response(
        'Error', '<div>Oh poo. <pre>{message}</pre></div>', message=message)


def format_html(template, **kwargs):
    """Like format(), but HTML-escapes all the parameters."""
    return template.format(
        **{key: html.escape(str(value)) for key, value in kwargs.items()})
