from src.sources.techstack.datadome import is_datadome_challenge, extract_datadome_params

# Real DataDome challenge body captured from G2
CHALLENGE_HTML = b"""<html lang="en"><head><title>g2.com</title>
<style>#cmsg{animation: A 1.5s;}@keyframes A{0%{opacity:0;}99%{opacity:0;}100%{opacity:1;}}</style>
<meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0">
<script data-cfasync="false">var dd={'rt':'c','cid':'AHrlqAAAAAMAMLKKDPRljmoA-xeMFg==','hsh':'229542D5C186C7F5A5BB092FBDD92B','t':'bv','qp':'','s':50168,'e':'888388f8cb4d5b60c8a7ff10fa95fb8c5349a6a8632fc9d5e7abcdb85fc7e695c1f23a153a70b67e060a3939b04be4ab','host':'geo.captcha-delivery.com','cookie':'i7~oh9PeMg~23dlVJ2QvoFCTFwpt_xg1GadnOuWmnssHQbVw3Vxyc9s2B_UGKSvT1Ou1sskXpYnfz5f3cj5eBIgR~3psMi14VVAc3ZukX5CuKM4ZdPqBYLQPyswLgrCj'}</script>
<script data-cfasync="false" src="https://ct.captcha-delivery.com/c.js"></script>
<iframe src="https://geo.captcha-delivery.com/captcha/?initialCid=AHrlqAAAAAMAMLKKDPRljmoA-xeMFg%3D%3D&hash=229542D5C186C7F5A5BB092FBDD92B&cid=i7~oh9PeMg~23dlVJ2QvoFCTFwpt_xg1GadnOuWmnssHQbVw3Vxyc9s2B_UGKSvT1Ou1sskXpYnfz5f3cj5eBIgR~3psMi14VVAc3ZukX5CuKM4ZdPqBYLQPyswLgrCj&t=bv&referer=https%3A%2F%2Fwww.g2.com%2Fsearch%3Fquery%3DDatabricks&s=50168&e=888388f8cb4d5b60c8a7ff10fa95fb8c5349a6a8632fc9d5e7abcdb85fc7e695c1f23a153a70b67e060a3939b04be4ab&dm=cd" sandbox="allow-scripts allow-same-origin allow-forms" allow="accelerometer; gyroscope; magnetometer" title="DataDome CAPTCHA" width="100%" height="100%" style="height:100vh;" frameborder="0" border="0" scrolling="yes"></iframe>
</body></html>"""

NORMAL_HTML = b"<html><body><h1>Welcome to G2</h1><p>Real content here.</p></body></html>"


def test_is_datadome_challenge_true():
    assert is_datadome_challenge(status=403, body=CHALLENGE_HTML) is True

def test_is_datadome_challenge_200_but_blocked():
    """DataDome sometimes returns 200 with a challenge body."""
    assert is_datadome_challenge(status=200, body=CHALLENGE_HTML) is True

def test_is_datadome_challenge_false_normal():
    assert is_datadome_challenge(status=200, body=NORMAL_HTML) is False

def test_is_datadome_challenge_false_403_other():
    """A 403 that's not DataDome should return False."""
    other_403 = b"<html><body>Forbidden</body></html>"
    assert is_datadome_challenge(status=403, body=other_403) is False

def test_extract_datadome_params():
    """Extract the dd={} JavaScript object parameters."""
    params = extract_datadome_params(CHALLENGE_HTML)
    assert params is not None
    assert params["cid"] == "AHrlqAAAAAMAMLKKDPRljmoA-xeMFg=="
    assert params["hsh"] == "229542D5C186C7F5A5BB092FBDD92B"
    assert params["t"] == "bv"
    assert params["s"] == 50168
    assert "e" in params

def test_extract_datadome_params_returns_none_on_normal():
    assert extract_datadome_params(NORMAL_HTML) is None

def test_extract_datadome_captcha_url():
    """Extract the full captcha URL from the iframe src."""
    from src.sources.techstack.datadome import extract_datadome_captcha_url
    url = extract_datadome_captcha_url(CHALLENGE_HTML)
    assert url is not None
    assert "geo.captcha-delivery.com/captcha/" in url
    assert "initialCid=" in url

def test_datadome_ip_banned():
    """When t=bv, the IP is banned and must be changed."""
    from src.sources.techstack.datadome import is_datadome_ip_banned
    params = extract_datadome_params(CHALLENGE_HTML)
    assert is_datadome_ip_banned(params) is True  # t=bv means banned

def test_datadome_ip_not_banned():
    from src.sources.techstack.datadome import is_datadome_ip_banned
    assert is_datadome_ip_banned({"t": "fe"}) is False
