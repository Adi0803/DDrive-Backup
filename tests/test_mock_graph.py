"""Self-tests for tests/mock_graph.py (the mock Graph / OneDrive server)."""

from __future__ import annotations

import base64
import http.client
import os
import random
import re
import socket
import time
from urllib.parse import quote, urlsplit

import pytest
import requests

from tests.mock_graph import (
    FRAGMENT_MULTIPLE,
    MockGraph,
    QuickXorHash,
    quickxorhash_reference,
)

TOKEN = "test-token"
AUTH = {"Authorization": "Bearer " + TOKEN}

# rclone quickxorhash test vectors (size, base64 input, base64 hash), extracted
# mechanically from rclone's backend/onedrive/quickxorhash/quickxorhash_test.go.
QXH_VECTORS = [
    (0, '', 'AAAAAAAAAAAAAAAAAAAAAAAAAAA='),
    (1, 'Sg==', 'SgAAAAAAAAAAAAAAAQAAAAAAAAA='),
    (2, 'tbQ=', 'taAFAAAAAAAAAAAAAgAAAAAAAAA='),
    (3, '0pZP', '0rDEEwAAAAAAAAAAAwAAAAAAAAA='),
    (4, 'jRRDVA==', 'jaDAEKgAAAAAAAAABAAAAAAAAAA='),
    (5, 'eAV52qE=', 'eChAHrQRCgAAAAAABQAAAAAAAAA='),
    (6, 'luBZlaT6', 'lgBHFipBCn0AAAAABgAAAAAAAAA='),
    (7, 'qaApEj66lw==', 'qQBFCiTgA11cAgAABwAAAAAAAAA='),
    (8, '/aNzzCFPS/A=', '/RjFHJgRgicsAR4ACAAAAAAAAAA='),
    (9, 'n6Neh7p6fFgm', 'nxiFFw6hCz3wAQsmCQAAAAAAAAA='),
    (10, 'J9iPGCbfZSTNyw==', 'J8DGIzBggm+UgQTNUgYAAAAAAAA='),
    (11, 'i+UZyUGJKh+ISbk=', 'iyhHBpIRhESo4AOIQ0IuAAAAAAA='),
    (12, 'h490d57Pqz5q2rtT', 'h3gEHe7giWeswgdq3MYupgAAAAA='),
    (13, 'vPgoDjOfO6fm71RxLw==', 'vMAHChwwg0/s4BTmdQcV4vACAAA='),
    (14, 'XoJ1AsoR4fDYJrDqYs4=', 'XhBEHQSgjAiEAx7YPgEs1CEGZwA='),
    (15, 'gQaybEqS/4UlDc8e4IJm', 'gDCALNigBEn8oxAlZ8AzPAAOQZg='),
    (16, '2fuxhBJXtpWFe8dOfdGeHw==', 'O9tHLAghgSvYohKFyMMxnNCHaHg='),
    (17, 'XBV6YKU9V7yMakZnFIxIkuU=', 'HbplHsBQih5cgReMQYMRzkABRiA='),
    (18, 'XJZSOiNO2bmfKnTKD7fztcQX', '/6ZArHQwAidkIxefQgEdlPGAW8w='),
    (19, 'g8VtAh+2Kf4k0kY5tzji2i2zmA==', 'wDNrgwHWAVukwB8kg4YRcnALHIg='),
    (20, 'T6LYJIfDh81JrAK309H2JMJTXis=', 'zBTHrspn3mEcohlJdIUAbjGNaNg='),
    (21, 'DWAAX5/CIfrmErgZa8ot6ZraeSbu', 'LR2Z0PjuRYGKQB/mhQAuMrAGZbQ='),
    (22, 'N9abi3qy/mC1THZuVLHPpx7SgwtLOA==', '1KTYttCBEen8Hwy1doId3ECFWDw='),
    (23, 'LlUe7wHerLqEtbSZLZgZa9u0m7hbiFs=', 'TqVZpxs3cN61BnuFvwUtMtECTGQ='),
    (24, 'bU2j/0XYdgfPFD4691jV0AOUEUPR4Z5E', 'bnLBiLpVgnxVkXhNsIAPdHAPLFQ='),
    (25, 'lScPwPsyUsH2T1Qsr31wXtP55Wqbe47Uyg==', 'VDMSy8eI26nBHCB0e8gVWPCKPsA='),
    (26, 'rJaKh1dLR1k+4hynliTZMGf8Nd4qKKoZiAM=', 'r7bjwkl8OYQeNaMcCY8fTmEJEmQ='),
    (27, 'pPsT0CPmHrd3Frsnva1pB/z1ytARLeHEYRCo', 'Rdg7rCcDomL59pL0s6GuTvqLVqQ='),
    (28, 'wSRChaqmrsnMrfB2yqI43eRWbro+f9kBvh+01w==', 'YTtloIi6frI7HX3vdLvE7I2iUOA='),
    (29, 'apL67KMIRxQeE9k1/RuW09ppPjbF1WeQpTjSWtI=', 'CIpedls+ZlSQ654fl+X26+Q7LVU='),
    (30, '53yx0/QgMTVb7OOzHRHbkS7ghyRc+sIXxi7XHKgT', 'zfJtLGFgR9DB3Q64fAFIp+S5iOY='),
    (31, 'PwXNnutoLLmxD8TTog52k8cQkukmT87TTnDipKLHQw==', 'PTaGs7yV3FUyBy/SfU6xJRlCJlI='),
    (32, 'NbYXsp5/K6mR+NmHwExjvWeWDJFnXTKWVlzYHoesp2E=', 'wjuAuWDiq04qDt1R8hHWDDcwVoQ='),
    (33, 'qQ70RB++JAR5ljNv3lJt1PpqETPsckopfonItu18Cr3E', 'FkJaeg/0Z5+euShYlLpE2tJh+Lo='),
    (34, 'RhzSatQTQ9/RFvpHyQa1WLdkr3nIk6MjJUma998YRtp44A==', 'SPN2D29reImAqJezlqV2DLbi8tk='),
    (35, 'DND1u1uZ5SqZVpRUk6NxSUdVo7IjjL9zs4A1evDNCDLcXWc=', 'S6lBk2hxI2SWBfn7nbEl7D19UUs='),
    (36, 'jEi62utFz69JMYHjg1iXy7oO6ZpZSLcVd2B+pjm6BGsv/CWi', 's0lYU9tr/bp9xsnrrjYgRS5EvV8='),
    (37, 'hfS3DZZnhy0hv7nJdXLv/oJOtIgAuP9SInt/v8KeuO4/IvVh4A==', 'CV+HQCdd2A/e/vdi12f2UU55GLA='),
    (38, 'EkPQAC6ymuRrYjIXD/LT/4Vb+7aTjYVZOHzC8GPCEtYDP0+T3Nc=', 'kE9H9sEmr3vHBYUiPbvsrcDgSEo='),
    (39, 'vtBOGIENG7yQ/N7xNWPNIgy66Gk/I2Ur/ZhdFNUK9/1FCZuu/KeS', '+Fgp3HBimtCzUAyiinj3pkarYTk='),
    (40, 'YnF4smoy9hox2jBlJ3VUa4qyCRhOZbWcmFGIiszTT4zAdYHsqJazyg==', 'arkIn+ELddmE8N34J9ydyFKW+9w='),
    (41, '0n7nl3YJtipy6yeUbVPWtc2h45WbF9u8hTz5tNwj3dZZwfXWkk+GN3g=', 'YJLNK7JR64j9aODWfqDvEe/u6NU='),
    (42, 'FnIIPHayc1pHkY4Lh8+zhWwG8xk6Knk/D3cZU1/fOUmRAoJ6CeztvMOL', '22RPOylMtdk7xO/QEQiMli4ql0k='),
    (43, 'J82VT7ND0Eg1MorSfJMUhn+qocF7PsUpdQAMrDiHJ2JcPZAHZ2nyuwjoKg==', 'pOR5eYfwCLRJbJsidpc1rIJYwtM='),
    (44, 'Zbu+78+e35ZIymV5KTDdub5McyI3FEO8fDxs62uWHQ9U3Oh3ZqgaZ30SnmQ=', 'DbvbTkgNTgWRqRidA9r1jhtUjro='),
    (45, 'lgybK3Da7LEeY5aeeNrqcdHvv6mD1W4cuQ3/rUj2C/CNcSI0cAMw6vtpVY3y', '700RQByn1lRQSSme9npQB/Ye+bY='),
    (46, 'jStZgKHv4QyJLvF2bYbIUZi/FscHALfKHAssTXkrV1byVR9eACwW9DNZQRHQwg==', 'uwN55He8xgE4g93dH9163xPew4U='),
    (47, 'V1PSud3giF5WW72JB/bgtltsWtEB5V+a+wUALOJOGuqztzVXUZYrvoP3XV++gM0=', 'U+3ZfUF/6mwOoHJcSHkQkckfTDA='),
    (48, 'VXs4t4tfXGiWAL6dlhEMm0YQF0f2w9rzX0CvIVeuW56o6/ec2auMpKeU2VeteEK5', 'sq24lSf7wXLH8eigHl07X+qPTps='),
    (49, 'bLUn3jLH+HFUsG3ptWTHgNvtr3eEv9lfKBf0jm6uhpqhRwtbEQ7Ovj/hYQf42zfdtQ==', 'uC8xrnopGiHebGuwgq607WRQyxQ='),
    (50, '4SVmjtXIL8BB8SfkbR5Cpaljm2jpyUfAhIBf65XmKxHlz9dy5XixgiE/q1lv+esZW/E=', 'wxZ0rxkMQEnRNAp8ZgEZLT4RdLM='),
    (51, 'pMljctlXeFUqbG3BppyiNbojQO3ygg6nZPeUZaQcVyJ+Clgiw3Q8ntLe8+02ZSfyCc39', 'aZEPmNvOXnTt7z7wt+ewV7QGMlg='),
    (52, 'C16uQlxsHxMWnV2gJhFPuJ2/guZ4N1YgmNvAwL1yrouGQtwieGx8WvZsmYRnX72JnbVtTw==', 'QtlSNqXhVij64MMhKJ3EsDFB/z8='),
    (53, '7ZVDOywvrl3L0GyKjjcNg2CcTI81n2CeUbzdYWcZOSCEnA/xrNHpiK01HOcGh3BbxuS4S6g=', '4NznNJc4nmXeApfiCFTq/H5LbHw='),
    (54, 'JXm2tTVqpYuuz2Cc+ZnPusUb8vccPGrzWK2oVwLLl/FjpFoxO9FxGlhnB08iu8Q/XQSdzHn+', 'IwE5+2pKNcK366I2k2BzZYPibSI='),
    (55, 'TiiU1mxzYBSGZuE+TX0l9USWBilQ7dEml5lLrzNPh75xmhjIK8SGqVAkvIMgAmcMB+raXdMPZg==', 'yECGHtgR128ScP4XlvF96eLbIBE='),
    (56, 'zz+Q4zi6wh0fCJUFU9yUOqEVxlIA93gybXHOtXIPwQQ44pW4fyh6BRgc1bOneRuSWp85hwlTJl8=', '+3Ef4D6yuoC8J+rbFqU1cegverE='),
    (57, 'sa6SHK9z/G505bysK5KgRO2z2cTksDkLoFc7sv0tWBmf2G2mCiozf2Ce6EIO+W1fRsrrtn/eeOAV', 'xZg1CwMNAjN0AIXw2yh4+1N3oos='),
    (58, '0qx0xdyTHhnKJ22IeTlAjRpWw6y2sOOWFP75XJ7cleGJQiV2kyrmQOST4DGHIL0qqA7sMOdzKyTViw==', 'bS0tRYPkP1Gfc+ZsBm9PMzPunG8='),
    (59, 'QuzaF0+5ooig6OLEWeibZUENl8EaiXAQvK9UjBEauMeuFFDCtNcGs25BDtJGGbX90gH4VZvCCDNCq4s=', 'rggokuJq1OGNOfB6aDp2g4rdPgw='),
    (60, '+wg2x23GZQmMLkdv9MeAdettIWDmyK6Wr+ba23XD+Pvvq1lIMn9QIQT4Z7QHJE3iC/ZMFgaId9VAyY3d', 'ahQbTmOdiKUNdhYRHgv5/Ky+Y6k='),
    (61, 'y0ydRgreRQwP95vpNP92ioI+7wFiyldHRbr1SfoPNdbKGFA0lBREaBEGNhf9yixmfE+Azo2AuROxb7Yc7g==', 'cJKFc0dXfiN4hMg1lcMf5E4gqvo='),
    (62, 'LxlVvGXSQlSubK8r0pGf9zf7s/3RHe75a2WlSXQf3gZFR/BtRnR7fCIcaG//CbGfodBFp06DBx/S9hUV8Bk=', 'NwuwhhRWX8QZ/vhWKWgQ1+rNomI='),
    (63, 'L+LSB8kmGMnHaWVA5P/+qFnfQliXvgJW7d2JGAgT6+koi5NQujFW1bwQVoXrBVyob/gBxGizUoJMgid5gGNo', 'ndX/KZBtFoeO3xKeo1ajO/Jy+rY='),
    (64, 'Mb7EGva2rEE5fENDL85P+BsapHEEjv2/siVhKjvAQe02feExVOQSkfmuYzU/kTF1MaKjPmKF/w+cbvwfdWL8aQ==', 'n1anP5NfvD4XDYWIeRPW3ZkPv1Y='),
    (111, 'jyibxJSzO6ZiZ0O1qe3tG/bvIAYssvukh9suIT5wEy1JBINVgPiqdsTW0cOpP0aUfP7mgqLfADkzI/m/GgCuVhr8oFLrOCoTx1/psBOWwhltCbhUx51Icm9aH8tY4Z3ccU+6BKpYQkLCy0B/A9Zc', 'hZfLIilSITC6N3e3tQ/iSgEzkto='),
    (128, 'ikwCorI7PKWz17EI50jZCGbV9JU2E8bXVfxNMg5zdmqSZ2NlsQPp0kqYIPjzwTg1MBtfWPg53k0h0P2naJNEVgrqpoHTfV2b3pJ4m0zYPTJmUX4Bg/lOxcnCxAYKU29Y5F0U8Quz7ZXFBEweftXxJ7RS4r6N7BzJrPsLhY7hgck=', 'imAoFvCWlDn4yVw3/oq1PDbbm6U='),
    (222, 'PfxMcUd0vIW6VbHG/uj/Y0W6qEoKmyBD0nYebEKazKaKG+UaDqBEcmQjbfQeVnVLuodMoPp7P7TR1htX5n2VnkHh22xDyoJ8C/ZQKiSNqQfXvh83judf4RVr9exJCud8Uvgip6aVZTaPrJHVjQhMCp/dEnGvqg0oN5OVkM2qqAXvA0teKUDhgNM71sDBVBCGXxNOR2bpbD1iM4dnuT0ey4L+loXEHTL0fqMeUcEi2asgImnlNakwenDzz0x57aBwyq3AspCFGB1ncX4yYCr/OaCcS5OKi/00WH+wNQU3', 'QX/YEpG0gDsmhEpCdWhsxDzsfVE='),
    (256, 'qwGf2ESubE5jOUHHyc94ORczFYYbc2OmEzo+hBIyzJiNwAzC8PvJqtTzwkWkSslgHFGWQZR2BV5+uYTrYT7HVwRM40vqfj0dBgeDENyTenIOL1LHkjtDKoXEnQ0mXAHoJ8PjbNC93zi5TovVRXTNzfGEs5dpWVqxUzb5lc7dwkyvOluBw482mQ4xrzYyIY1t+//OrNi1ObGXuUw2jBQOFfJVj2Y6BOyYmfB1y36eBxi3zxeG5d5NYjm2GSh6e08QMAwu3zrINcqIzLOuNIiGXBtl7DjKt7b5wqi4oFiRpZsCyx2smhSrdrtK/CkdU6nDN+34vSR/M8rZpWQdBE7a8g==', 'WYT9JY3JIo/pEBp+tIM6Gt2nyTM='),
    (333, 'w0LGhqU1WXFbdavqDE4kAjEzWLGGzmTNikzqnsiXHx2KRReKVTxkv27u3UcEz9+lbMvYl4xFf2Z4aE1xRBBNd1Ke5C0zToSaYw5o4B/7X99nKK2/XaUX1byLow2aju2XJl2OpKpJg+tSJ2fmjIJTkfuYUz574dFX6/VXxSxwGH/xQEAKS5TCsBK3CwnuG1p5SAsQq3gGVozDWyjEBcWDMdy8/AIFrj/y03Lfc/RNRCQTAfZbnf2QwV7sluw4fH3XJr07UoD0YqN+7XZzidtrwqMY26fpLZnyZjnBEt1FAZWO7RnKG5asg8xRk9YaDdedXdQSJAOy6bWEWlABj+tVAigBxavaluUH8LOj+yfCFldJjNLdi90fVHkUD/m4Mr5OtmupNMXPwuG3EQlqWUVpQoYpUYKLsk7a5Mvg6UFkiH596y5IbJEVCI1Kb3D1', 'e3+wo77iKcILiZegnzyUNcjCdoQ='),
]

# Where the original Go file lives on the machine that generated this suite;
# override with QXH_TEST_GO.  The test that re-parses it is skipped if absent.
QXH_GO_FILE = os.environ.get(
    "QXH_TEST_GO",
    "/tmp/claude-0/-home-user-DDrive-Backup/97c1053e-9b85-5494-ae74-cdf20979d671/scratchpad/ext/qxh_test.go",
)


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------


@pytest.fixture
def make_graph():
    started = []

    def _make(**kw):
        g = MockGraph(**kw)
        g.start()
        started.append(g)
        return g

    yield _make
    for g in started:
        g.stop()
    for g in started:
        assert g.internal_errors == []


@pytest.fixture
def graph(make_graph):
    return make_graph()


@pytest.fixture
def api():
    s = requests.Session()
    s.headers.update(AUTH)
    yield s
    s.close()


def _err_code(resp):
    return resp.json()["error"]["code"]


def _children_url(g, item_id):
    return "%s/drives/%s/items/%s/children" % (g.base_url, g.drive_id, item_id)


def _put_url(g, parent_id, encoded_name):
    return "%s/drives/%s/items/%s:/%s:/content" % (g.base_url, g.drive_id, parent_id, encoded_name)


def _session_url(g, parent_id, name):
    return "%s/drives/%s/items/%s:/%s:/createUploadSession" % (
        g.base_url, g.drive_id, parent_id, quote(name, safe=""))


def _put_fragment(url, data, start, total, **kw):
    end = start + len(data) - 1
    return requests.put(url, data=data, headers={
        "Content-Length": str(len(data)),
        "Content-Range": "bytes %d-%d/%d" % (start, end, total),
    }, **kw)


def _naive_qxh(data: bytes) -> str:
    """Bit-level definition: byte i is XORed into a 160-bit circular register
    at bit (11*i) mod 160; length XORed into the last 8 bytes."""
    width = 160
    reg = 0
    full = (1 << width) - 1
    for i, b in enumerate(data):
        pos = (11 * i) % width
        v = b << pos
        reg ^= (v | (v >> width)) & full
    out = bytearray(reg.to_bytes(20, "little"))
    for i, lb in enumerate(len(data).to_bytes(8, "little")):
        out[12 + i] ^= lb
    return base64.b64encode(bytes(out)).decode()


# --------------------------------------------------------------------------
# QuickXorHash
# --------------------------------------------------------------------------


def test_qxh_vector_table_complete():
    assert len(QXH_VECTORS) == 70
    for size, inp, _ in QXH_VECTORS:
        assert len(base64.b64decode(inp)) == size


@pytest.mark.parametrize("size,inp,expected", QXH_VECTORS, ids=[str(v[0]) for v in QXH_VECTORS])
def test_qxh_rclone_vectors(size, inp, expected):
    assert quickxorhash_reference(base64.b64decode(inp)) == expected


def test_qxh_vectors_from_go_file():
    if not os.path.exists(QXH_GO_FILE):
        pytest.skip("qxh_test.go not available")
    src = open(QXH_GO_FILE, encoding="utf-8").read()
    found = re.findall(r"\{(\d+),\s*`([^`]*)`,\s*\"([^\"]*)\"\}", src)
    assert len(found) == len(QXH_VECTORS)
    for size, inp, expected in found:
        data = base64.b64decode("".join(inp.split()))
        assert len(data) == int(size)
        assert quickxorhash_reference(data) == expected, size


@pytest.mark.parametrize("block", [1, 2, 4, 7, 8, 16, 32, 64, 128, 256, 512])
def test_qxh_streaming_blocks(block):
    for size, inp, expected in QXH_VECTORS:
        data = base64.b64decode(inp)
        h = QuickXorHash()
        for i in range(0, len(data), block):
            h.update(data[i:i + block])
        assert h.b64digest() == expected, (size, block)


def test_qxh_matches_bit_level_definition():
    rnd = random.Random(1234)
    for n in [0, 1, 19, 20, 21, 159, 160, 161, 333, 1000, 4096, 10007]:
        data = bytes(rnd.getrandbits(8) for _ in range(n))
        assert quickxorhash_reference(data) == _naive_qxh(data), n
        h = QuickXorHash()
        pos = 0
        while pos < n:  # odd-sized streaming updates
            step = rnd.randint(1, 400)
            h.update(data[pos:pos + step])
            pos += step
        assert h.b64digest() == _naive_qxh(data), n


# --------------------------------------------------------------------------
# auth, basics, routing
# --------------------------------------------------------------------------


def test_auth_required(graph):
    r = requests.get(graph.base_url + "/me/drive")
    assert r.status_code == 401
    assert _err_code(r) == "InvalidAuthenticationToken"
    assert "message" in r.json()["error"]
    r = requests.get(graph.base_url + "/me/drive", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401 and _err_code(r) == "InvalidAuthenticationToken"
    r = requests.get(graph.base_url + "/drives/%s/root" % graph.drive_id,
                     headers={"Authorization": "Basic Zm9vOmJhcg=="})
    assert r.status_code == 401

    me = requests.get(graph.base_url + "/me", headers=AUTH)
    assert me.status_code == 200
    assert me.json()["displayName"] == "Test User"
    assert me.json()["userPrincipalName"] == "test@example.com"
    assert me.json()["id"]

    d = requests.get(graph.base_url + "/me/drive", headers=AUTH).json()
    assert d["id"] == graph.drive_id and d["driveType"] == "business" and d["name"] == "OneDrive"
    assert d["quota"]["total"] == 1 << 40 and d["quota"]["state"] == "normal"
    assert d["quota"]["used"] == 0 and d["quota"]["remaining"] == 1 << 40

    graph.add_file("x/y.bin", b"12345")
    d = requests.get(graph.base_url + "/me/drive", headers=AUTH).json()
    assert d["quota"]["used"] == 5 == graph.quota_used
    assert d["quota"]["remaining"] == (1 << 40) - 5


def test_custom_valid_tokens(make_graph):
    g = make_graph(valid_tokens={"a", "b"})
    assert requests.get(g.base_url + "/me", headers={"Authorization": "Bearer b"}).status_code == 200
    assert requests.get(g.base_url + "/me", headers=AUTH).status_code == 401


def test_unknown_routes_are_400(graph, api):
    for url in ["/nope", "/me/drive/bogus", "/drives/%s/items/%s/frobnicate" % (graph.drive_id, graph.root_id)]:
        r = api.get(graph.base_url + url)
        assert r.status_code == 400, url
        assert _err_code(r) == "invalidRequest"
        assert set(r.json()["error"]) >= {"code", "message"}
    r = api.get(graph.origin + "/v2.0/me")
    assert r.status_code == 400 and _err_code(r) == "invalidRequest"
    r = api.get(graph.base_url + "/drives/b!doesnotexist/root")
    assert r.status_code == 404 and _err_code(r) == "itemNotFound"


def test_get_root_item_and_by_path(graph, api):
    f = graph.add_file("D-Drive-Backup/Sub Dir/file.txt", b"hello", mtime="2020-05-06T07:08:09Z")
    root = api.get("%s/drives/%s/root" % (graph.base_url, graph.drive_id)).json()
    assert root["id"] == graph.root_id and "root" in root and root["folder"]["childCount"] == 1
    assert root["size"] == 5

    for url in [
        "%s/me/drive/root:/D-Drive-Backup/Sub%%20Dir/file.txt" % graph.base_url,
        "%s/me/drive/root:/d-drive-backup/sub%%20dir/FILE.TXT:" % graph.base_url,
        "%s/drives/%s/root:/D-Drive-Backup/Sub%%20Dir/file.txt:" % (graph.base_url, graph.drive_id),
    ]:
        r = api.get(url)
        assert r.status_code == 200, url
        assert r.json()["id"] == f["id"]
    item = r.json()
    assert item["name"] == "file.txt" and item["size"] == 5
    assert item["fileSystemInfo"]["lastModifiedDateTime"] == "2020-05-06T07:08:09Z"
    assert item["file"]["hashes"]["quickXorHash"] == quickxorhash_reference(b"hello")
    assert item["file"]["mimeType"] == "application/octet-stream"
    assert item["parentReference"]["path"] == "/drive/root:/D-Drive-Backup/Sub Dir"
    assert item["parentReference"]["driveId"] == graph.drive_id
    assert item["eTag"] and item["cTag"]
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", item["lastModifiedDateTime"])

    folder = api.get("%s/me/drive/root:/D-Drive-Backup" % graph.base_url).json()
    assert folder["folder"]["childCount"] == 1 and folder["size"] == 5
    assert "cTag" not in folder and "file" not in folder
    assert folder["parentReference"]["path"] == "/drive/root:"

    r = api.get("%s/me/drive/root:/D-Drive-Backup/missing.txt" % graph.base_url)
    assert r.status_code == 404 and _err_code(r) == "itemNotFound"
    r = api.get("%s/drives/%s/items/%s" % (graph.base_url, graph.drive_id, "01NOPE"))
    assert r.status_code == 404 and _err_code(r) == "itemNotFound"
    r = api.get("%s/drives/%s/items/%s" % (graph.base_url, graph.drive_id, f["id"]))
    assert r.status_code == 200 and r.json()["name"] == "file.txt"
    assert graph.get_item_by_path("d-drive-backup/SUB DIR/file.TXT")["id"] == f["id"]
    assert graph.get_item_by_path("nope") is None


# --------------------------------------------------------------------------
# children listing
# --------------------------------------------------------------------------


def test_children_paging(make_graph, api):
    g = make_graph(page_size=2)
    for name in ["c.txt", "A.txt", "b.txt", "e.txt"]:
        g.add_file("D-Drive-Backup/" + name, name.encode())
    g.add_folder("D-Drive-Backup/d-folder")
    parent = g.get_item_by_path("D-Drive-Backup")

    def list_all(url, params=None):
        names, pages = [], 0
        r = api.get(url, params=params)
        while True:
            assert r.status_code == 200, r.text
            body = r.json()
            pages += 1
            assert len(body["value"]) <= 2
            names.extend(v["name"] for v in body["value"])
            for v in body["value"]:
                if params and "$select" in params:
                    assert set(v) <= {"id", "name", "size"}, v
                    assert {"id", "name"} <= set(v)
            nxt = body.get("@odata.nextLink")
            if not nxt:
                return names, pages
            assert nxt.startswith(g.origin + "/v1.0/")
            r = api.get(nxt)

    expected = ["A.txt", "b.txt", "c.txt", "d-folder", "e.txt"]
    names, pages = list_all(_children_url(g, parent["id"]))
    assert names == expected and pages == 3
    names2, _ = list_all(_children_url(g, parent["id"]), {"$select": "name,size", "$orderby": "name desc",
                                                         "$expand": "thumbnails", "$filter": "x"})
    assert names2 == expected  # stable, unknown $-params ignored
    names3, _ = list_all("%s/me/drive/root:/D-Drive-Backup:/children" % g.base_url)
    assert names3 == expected
    root_names, _ = list_all("%s/me/drive/root/children" % g.base_url)
    assert root_names == ["D-Drive-Backup"]
    # $top cannot exceed the server page size but can shrink it
    r = api.get(_children_url(g, parent["id"]), params={"$top": "1"})
    assert len(r.json()["value"]) == 1 and "@odata.nextLink" in r.json()
    r = api.get(_children_url(g, "01MISSING"))
    assert r.status_code == 404


# --------------------------------------------------------------------------
# folder creation
# --------------------------------------------------------------------------


def test_folder_create_and_conflicts(graph, api):
    url = _children_url(graph, graph.root_id)
    r = api.post(url, json={"name": "Photos", "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})
    assert r.status_code == 201, r.text
    photos = r.json()
    assert photos["name"] == "Photos" and photos["folder"]["childCount"] == 0

    r = api.post(url, json={"name": "photos", "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})
    assert r.status_code == 409 and _err_code(r) == "nameAlreadyExists"
    r = api.post(url, json={"name": "PHOTOS", "folder": {}})  # default is fail
    assert r.status_code == 409 and _err_code(r) == "nameAlreadyExists"

    r = api.post(url, json={"name": "photos", "folder": {}, "@microsoft.graph.conflictBehavior": "replace"})
    assert r.status_code in (200, 201) and r.json()["id"] == photos["id"]
    assert r.json()["name"] == "Photos"

    r = api.post(url, json={"name": "photos", "folder": {}, "@microsoft.graph.conflictBehavior": "rename"})
    assert r.status_code == 201 and r.json()["name"] == "photos 1"

    r = api.post(_children_url(graph, photos["id"]), json={"name": "2024", "folder": {}})
    assert r.status_code == 201
    assert graph.folders() == {"Photos", "photos 1", "Photos/2024"}

    r = api.post(_children_url(graph, "01NOPE"), json={"name": "x", "folder": {}})
    assert r.status_code == 404
    r = api.post(url, json={"name": "bad:name", "folder": {}})
    assert r.status_code == 400 and _err_code(r) == "invalidRequest"
    r = api.post(url, data=b"not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400


# --------------------------------------------------------------------------
# simple upload
# --------------------------------------------------------------------------


SPECIAL_NAMES = [
    ("a#b.txt", "a%23b.txt"),
    ("50%20off.pdf", "50%2520off.pdf"),
    ("naïve.txt", "na%C3%AFve.txt"),
    ("space name.txt", "space%20name.txt"),
]


def test_simple_upload_special_names(graph, api):
    parent = graph.add_folder("D-Drive-Backup")
    for i, (name, encoded) in enumerate(SPECIAL_NAMES):
        assert quote(name, safe="") == encoded
        content = ("content %d" % i).encode()
        r = api.put(_put_url(graph, parent["id"], encoded), data=content)
        assert r.status_code == 201, (name, r.text)
        item = r.json()
        assert item["name"] == name
        assert item["size"] == len(content)
        assert item["file"]["hashes"]["quickXorHash"] == quickxorhash_reference(content)
        assert graph.tree()["D-Drive-Backup/" + name] == content
        r = api.get("%s/me/drive/root:/D-Drive-Backup/%s" % (graph.base_url, encoded))
        assert r.status_code == 200 and r.json()["id"] == item["id"]

    # the raw path is logged exactly as sent (decoded exactly once by the server)
    assert any(e["path"].endswith("/50%2520off.pdf:/content") for e in graph.requests_log)
    assert "D-Drive-Backup/50 off.pdf" not in graph.tree()

    # replacing with a differently-cased name keeps id and original case
    first = graph.get_item_by_path("D-Drive-Backup/a#b.txt")
    r = api.put(_put_url(graph, parent["id"], "A%23B.TXT"), data=b"new")
    assert r.status_code == 200
    assert r.json()["id"] == first["id"] and r.json()["name"] == "a#b.txt"
    assert r.json()["cTag"] != first["cTag"] and r.json()["eTag"] != first["eTag"]
    assert graph.tree()["D-Drive-Backup/a#b.txt"] == b"new"
    assert sorted(graph.tree()) == sorted("D-Drive-Backup/" + n for n, _ in SPECIAL_NAMES)

    # empty body is fine
    r = api.put(_put_url(graph, parent["id"], "empty.txt"), data=b"")
    assert r.status_code == 201 and r.json()["size"] == 0
    assert r.json()["file"]["hashes"]["quickXorHash"] == "AAAAAAAAAAAAAAAAAAAAAAAAAAA="

    # replace existing by item id
    r = api.put("%s/drives/%s/items/%s/content" % (graph.base_url, graph.drive_id, first["id"]), data=b"v3")
    assert r.status_code == 200 and graph.tree()["D-Drive-Backup/a#b.txt"] == b"v3"

    # conflictBehavior=fail via query string
    r = api.put(_put_url(graph, parent["id"], "empty.txt"), data=b"x",
                params={"@microsoft.graph.conflictBehavior": "fail"})
    assert r.status_code == 409

    # invalid / reserved names, and a folder in the way
    for bad in ["bad%3Aname.txt", "q%3F.txt", "desktop.ini", "~%24lock.docx", "%20lead.txt"]:
        r = api.put(_put_url(graph, parent["id"], bad), data=b"x")
        assert r.status_code == 400, bad
    graph.add_folder("D-Drive-Backup/dir")
    r = api.put(_put_url(graph, parent["id"], "DIR"), data=b"x")
    assert r.status_code == 409 and _err_code(r) == "nameAlreadyExists"

    # path-based upload under root creates missing folders
    r = api.put("%s/me/drive/root:/New%%20Top/sub/f.bin:/content" % graph.base_url, data=b"abc")
    assert r.status_code == 201
    assert graph.tree()["New Top/sub/f.bin"] == b"abc"


def test_simple_upload_too_large(graph, api):
    graph.simple_upload_limit = 10
    p = graph.add_folder("big")
    r = api.put(_put_url(graph, p["id"], "f.bin"), data=b"x" * 11)
    assert r.status_code == 413
    assert "error" in r.json()
    r = api.put(_put_url(graph, p["id"], "f.bin"), data=b"x" * 10)
    assert r.status_code == 201


def test_simple_upload_chunked_body(graph, api):
    p = graph.add_folder("c")

    def gen():
        yield b"abc"
        yield b"def"

    r = api.put(_put_url(graph, p["id"], "chunked.bin"), data=gen())
    assert r.status_code == 201
    assert graph.tree()["c/chunked.bin"] == b"abcdef"


def test_quota_exceeded(graph, api):
    graph.quota_total = 100
    p = graph.add_folder("q")
    assert api.put(_put_url(graph, p["id"], "a"), data=b"x" * 60).status_code == 201
    r = api.put(_put_url(graph, p["id"], "b"), data=b"x" * 60)
    assert r.status_code == 507 and _err_code(r) == "quotaLimitReached"
    d = api.get(graph.base_url + "/me/drive").json()
    assert d["quota"]["used"] == 60 and d["quota"]["remaining"] == 40


# --------------------------------------------------------------------------
# upload sessions
# --------------------------------------------------------------------------


def _new_session(g, api, parent_id, fname, **item):
    body = {"item": dict({"@microsoft.graph.conflictBehavior": "replace"}, **item)}
    r = api.post(_session_url(g, parent_id, fname), json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_upload_session_happy_path(graph, api):
    parent = graph.add_folder("D-Drive-Backup/big")
    fsi = {"createdDateTime": "2019-01-02T03:04:05Z", "lastModifiedDateTime": "2021-06-07T08:09:10.123Z"}
    sess = _new_session(graph, api, parent["id"], "Movie #1.mkv", fileSystemInfo=fsi, name="Movie #1.mkv")
    url = sess["uploadUrl"]
    assert url.startswith(graph.origin + "/upload/") and "/v1.0" not in url
    assert sess["nextExpectedRanges"] == ["0-"] and sess["expirationDateTime"].endswith("Z")

    rnd = random.Random(7)
    data = bytes(rnd.getrandbits(8) for _ in range(FRAGMENT_MULTIPLE * 2 + 12345))
    total = len(data)
    r = _put_fragment(url, data[:FRAGMENT_MULTIPLE], 0, total)
    assert r.status_code == 202 and r.json()["nextExpectedRanges"] == ["%d-" % FRAGMENT_MULTIPLE]
    r = _put_fragment(url, data[FRAGMENT_MULTIPLE:2 * FRAGMENT_MULTIPLE], FRAGMENT_MULTIPLE, total)
    assert r.status_code == 202 and r.json()["nextExpectedRanges"] == ["%d-" % (2 * FRAGMENT_MULTIPLE)]
    assert "expirationDateTime" in r.json()
    r = _put_fragment(url, data[2 * FRAGMENT_MULTIPLE:], 2 * FRAGMENT_MULTIPLE, total)
    assert r.status_code == 201, r.text
    item = r.json()
    assert item["name"] == "Movie #1.mkv" and item["size"] == total
    assert item["fileSystemInfo"] == {"createdDateTime": "2019-01-02T03:04:05Z",
                                      "lastModifiedDateTime": "2021-06-07T08:09:10Z"}
    assert item["file"]["hashes"]["quickXorHash"] == quickxorhash_reference(data) == _naive_qxh(data)
    assert item["parentReference"]["id"] == parent["id"]
    assert graph.tree()["D-Drive-Backup/big/Movie #1.mkv"] == data
    assert requests.get(url).status_code == 404  # committed sessions are gone
    assert not any("Authorization" in e["headers"] for e in graph.requests_log if e["path"].startswith("/upload/"))

    # a second session replaces the file, keeping id and name case
    sess2 = _new_session(graph, api, parent["id"], "MOVIE #1.MKV")
    r = _put_fragment(sess2["uploadUrl"], b"tiny", 0, 4)
    assert r.status_code == 200
    assert r.json()["id"] == item["id"] and r.json()["name"] == "Movie #1.mkv"
    assert graph.tree()["D-Drive-Backup/big/Movie #1.mkv"] == b"tiny"
    # no fileSystemInfo given -> lastModified reset to "now"
    assert r.json()["fileSystemInfo"]["lastModifiedDateTime"] != "2021-06-07T08:09:10Z"


def test_upload_session_status_and_416(graph, api):
    parent = graph.add_folder("s")
    data = os.urandom(FRAGMENT_MULTIPLE * 2 + 5)
    total = len(data)
    url = _new_session(graph, api, parent["id"], "f.bin")["uploadUrl"]

    st = requests.get(url)
    assert st.status_code == 200 and st.json()["nextExpectedRanges"] == ["0-"]
    assert "expirationDateTime" in st.json()

    assert _put_fragment(url, data[:FRAGMENT_MULTIPLE], 0, total).status_code == 202
    st = requests.get(url)
    assert st.status_code == 200 and st.json()["nextExpectedRanges"] == ["%d-" % FRAGMENT_MULTIPLE]

    # re-sent fragment -> 416
    r = _put_fragment(url, data[:FRAGMENT_MULTIPLE], 0, total)
    assert r.status_code == 416 and _err_code(r) == "invalidRange"
    # skipping ahead -> 416 as well
    r = _put_fragment(url, data[2 * FRAGMENT_MULTIPLE:], 2 * FRAGMENT_MULTIPLE, total)
    assert r.status_code == 416
    # different total -> 400
    r = _put_fragment(url, data[FRAGMENT_MULTIPLE:2 * FRAGMENT_MULTIPLE], FRAGMENT_MULTIPLE, total + 1)
    assert r.status_code == 400
    # missing Content-Range -> 400
    r = requests.put(url, data=b"abc")
    assert r.status_code == 400
    # Content-Range length mismatch -> 400
    r = requests.put(url, data=b"abc", headers={"Content-Range": "bytes %d-%d/%d" % (
        FRAGMENT_MULTIPLE, FRAGMENT_MULTIPLE + 9, total)})
    assert r.status_code == 400
    assert requests.get(url).json()["nextExpectedRanges"] == ["%d-" % FRAGMENT_MULTIPLE]

    assert _put_fragment(url, data[FRAGMENT_MULTIPLE:2 * FRAGMENT_MULTIPLE], FRAGMENT_MULTIPLE, total).status_code == 202
    r = _put_fragment(url, data[2 * FRAGMENT_MULTIPLE:], 2 * FRAGMENT_MULTIPLE, total)
    assert r.status_code == 201
    assert graph.tree()["s/f.bin"] == data


def test_upload_session_non_multiple_fragment(graph, api):
    parent = graph.add_folder("s")
    url = _new_session(graph, api, parent["id"], "f.bin")["uploadUrl"]
    data = os.urandom(5000)
    r = _put_fragment(url, data[:1000], 0, len(data))
    assert r.status_code == 400 and _err_code(r) == "invalidRequest"
    assert requests.get(url).json()["nextExpectedRanges"] == ["0-"]
    # a single final fragment of any size is fine
    r = _put_fragment(url, data, 0, len(data))
    assert r.status_code == 201


def test_upload_url_rejects_authorization(graph, api):
    parent = graph.add_folder("s")
    url = _new_session(graph, api, parent["id"], "f.bin")["uploadUrl"]
    r = requests.put(url, data=b"abc", headers=dict(AUTH, **{"Content-Range": "bytes 0-2/3"}))
    assert r.status_code == 401
    assert "error" in r.json()
    assert requests.get(url).json()["nextExpectedRanges"] == ["0-"]
    # the session survives; tempauth in the URL is required
    stripped = url.split("?")[0]
    assert _put_fragment(stripped, b"abc", 0, 3).status_code == 401
    assert _put_fragment(url, b"abc", 0, 3).status_code == 201


def test_upload_session_cancel_and_expiry(graph, api):
    parent = graph.add_folder("s")
    url = _new_session(graph, api, parent["id"], "f.bin")["uploadUrl"]
    assert len(graph.upload_sessions()) == 1
    r = requests.delete(url)
    assert r.status_code == 204
    assert requests.get(url).status_code == 404
    assert _put_fragment(url, b"abc", 0, 3).status_code == 404
    assert graph.upload_sessions() == []

    url = _new_session(graph, api, parent["id"], "g.bin")["uploadUrl"]
    graph.expire_upload_sessions()
    assert requests.get(url).status_code == 404


def test_create_session_errors(graph, api):
    parent = graph.add_folder("s")
    graph.add_folder("s/taken")
    graph.add_file("s/exists.bin", b"old")

    r = api.post(_session_url(graph, "01DOESNOTEXIST", "f.bin"), json={"item": {}})
    assert r.status_code == 404 and _err_code(r) == "itemNotFound"
    r = api.post(_session_url(graph, parent["id"], "taken"),
                 json={"item": {"@microsoft.graph.conflictBehavior": "replace"}})
    assert r.status_code == 409 and _err_code(r) == "nameAlreadyExists"
    # default conflictBehavior for createUploadSession is "fail" (per docs)
    r = api.post(_session_url(graph, parent["id"], "EXISTS.bin"), json={"item": {}})
    assert r.status_code == 409
    # rename
    url = _new_session(graph, api, parent["id"], "exists.bin",
                       **{"@microsoft.graph.conflictBehavior": "rename"})["uploadUrl"]
    r = _put_fragment(url, b"new", 0, 3)
    assert r.status_code == 201 and r.json()["name"] == "exists 1.bin"
    # bad fileSystemInfo
    r = api.post(_session_url(graph, parent["id"], "x.bin"),
                 json={"item": {"fileSystemInfo": {"lastModifiedDateTime": "yesterday"}}})
    assert r.status_code == 400
    # session without auth
    r = requests.post(_session_url(graph, parent["id"], "x.bin"), json={"item": {}})
    assert r.status_code == 401


def test_upload_session_commit_conflict_can_retry(graph, api):
    parent = graph.add_folder("s")
    url = _new_session(graph, api, parent["id"], "late.bin",
                       **{"@microsoft.graph.conflictBehavior": "fail"})["uploadUrl"]
    graph.add_file("s/late.bin", b"someone else")
    r = _put_fragment(url, b"mine", 0, 4)
    assert r.status_code == 409
    assert requests.get(url).json()["nextExpectedRanges"] == ["0-"]
    api.delete("%s/me/drive/root:/s/late.bin" % graph.base_url)
    r = _put_fragment(url, b"mine", 0, 4)
    assert r.status_code == 201 and graph.tree()["s/late.bin"] == b"mine"


def test_upload_session_for_existing_item(graph, api):
    f = graph.add_file("s/f.bin", b"old")
    r = api.post("%s/drives/%s/items/%s/createUploadSession" % (graph.base_url, graph.drive_id, f["id"]),
                 json={"item": {"fileSystemInfo": {"lastModifiedDateTime": "2022-02-02T02:02:02Z"}}})
    assert r.status_code == 200
    r = _put_fragment(r.json()["uploadUrl"], b"newer", 0, 5)
    assert r.status_code == 200 and r.json()["id"] == f["id"]
    assert r.json()["fileSystemInfo"]["lastModifiedDateTime"] == "2022-02-02T02:02:02Z"


# --------------------------------------------------------------------------
# delete / patch / download
# --------------------------------------------------------------------------


def test_delete_moves_to_recycle_bin(graph, api):
    f = graph.add_file("D-Drive-Backup/a/one.txt", b"1")
    graph.add_file("D-Drive-Backup/a/sub/two.txt", b"22")
    keep = graph.add_file("D-Drive-Backup/keep.txt", b"k")
    r = api.delete("%s/drives/%s/items/%s" % (graph.base_url, graph.drive_id, f["id"]))
    assert r.status_code == 204 and r.content == b""
    assert graph.recycle_bin[-1]["path"] == "D-Drive-Backup/a/one.txt"
    assert graph.recycle_bin[-1]["content"] == b"1"
    assert "D-Drive-Backup/a/one.txt" not in graph.tree()
    r = api.get("%s/drives/%s/items/%s" % (graph.base_url, graph.drive_id, f["id"]))
    assert r.status_code == 404
    r = api.delete("%s/drives/%s/items/%s" % (graph.base_url, graph.drive_id, f["id"]))
    assert r.status_code == 404

    folder = graph.get_item_by_path("D-Drive-Backup/a")
    r = api.delete("%s/drives/%s/items/%s" % (graph.base_url, graph.drive_id, folder["id"]))
    assert r.status_code == 204
    entry = graph.recycle_bin[-1]
    assert entry["path"] == "D-Drive-Backup/a" and entry["type"] == "folder"
    assert entry["files"] == {"D-Drive-Backup/a/sub/two.txt": b"22"}
    assert graph.tree() == {"D-Drive-Backup/keep.txt": b"k"}
    assert graph.folders() == {"D-Drive-Backup"}
    assert len(graph.recycle_bin) == 2
    r = api.delete("%s/drives/%s/root" % (graph.base_url, graph.drive_id))
    assert r.status_code == 403
    assert graph.get_item_by_path("D-Drive-Backup/keep.txt")["id"] == keep["id"]


def test_patch_file_system_info_and_rename(graph, api):
    f = graph.add_file("p/f.txt", b"x")
    url = "%s/drives/%s/items/%s" % (graph.base_url, graph.drive_id, f["id"])
    r = api.patch(url, json={"fileSystemInfo": {"lastModifiedDateTime": "2010-10-10T10:10:10+02:00"}})
    assert r.status_code == 200
    assert r.json()["fileSystemInfo"]["lastModifiedDateTime"] == "2010-10-10T08:10:10Z"
    assert r.json()["cTag"] == f["cTag"] and r.json()["eTag"] != f["eTag"]
    assert graph.get_item_by_path("p/f.txt")["fileSystemInfo"]["lastModifiedDateTime"] == "2010-10-10T08:10:10Z"
    r = api.patch(url, json={"name": "g.txt"})
    assert r.status_code == 200 and graph.tree() == {"p/g.txt": b"x"}
    r = api.patch(url, json={"fileSystemInfo": {"lastModifiedDateTime": "garbage"}})
    assert r.status_code == 400
    r = api.patch(url, json={"name": "h.txt"}, headers={"If-Match": '"{00000000-0000},1"'})
    assert r.status_code == 412


def test_download_content(graph, api):
    f = graph.add_file("dl/f.bin", b"0123456789")
    r = api.get("%s/drives/%s/items/%s/content" % (graph.base_url, graph.drive_id, f["id"]))
    assert r.status_code == 200 and r.content == b"0123456789"
    assert r.history and r.history[0].status_code == 302
    r = requests.get(f["@microsoft.graph.downloadUrl"], headers={"Range": "bytes=2-4"})
    assert r.status_code == 206 and r.content == b"234"


def test_delta(make_graph, api):
    g = make_graph(page_size=2)
    g.add_file("a/1.txt", b"1")
    g.add_file("a/2.txt", b"2")
    gone = g.add_file("b/3.txt", b"3")
    url = "%s/me/drive/root/delta" % g.base_url
    seen, delta_link = [], None
    while url:
        body = api.get(url).json()
        seen.extend(body["value"])
        url = body.get("@odata.nextLink")
        delta_link = body.get("@odata.deltaLink")
    assert delta_link
    names = [v["name"] for v in seen]
    assert names[0] == "root" and set(names) == {"root", "a", "1.txt", "2.txt", "b", "3.txt"}
    assert all("cTag" not in v for v in seen)

    g.add_file("a/new.txt", b"n")
    api.delete("%s/drives/%s/items/%s" % (g.base_url, g.drive_id, gone["id"]))
    url, changes = delta_link, []
    while url:
        body = api.get(url).json()
        changes.extend(body["value"])
        url = body.get("@odata.nextLink")
    assert any(v.get("name") == "new.txt" for v in changes)
    assert any(v["id"] == gone["id"] and "deleted" in v for v in changes)
    assert not any(v.get("name") == "2.txt" for v in changes)
    latest = api.get("%s/me/drive/root/delta" % g.base_url, params={"token": "latest"}).json()
    assert latest["value"] == [] and "token=" in latest["@odata.deltaLink"]


# --------------------------------------------------------------------------
# fault injection
# --------------------------------------------------------------------------


def test_fail_next_with_retry_after(graph, api):
    p = graph.add_folder("f")
    graph.fail_next("PUT", r"/content$", 429, headers={"Retry-After": "3"})
    r = api.put(_put_url(graph, p["id"], "x.txt"), data=b"abc")
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "3"
    assert _err_code(r) == "activityLimitReached"
    assert "f/x.txt" not in graph.tree()
    assert graph.requests_log[-1]["status"] == 429 and graph.requests_log[-1]["fault"] == "fail"
    assert graph.requests_log[-1]["body_len"] == 3

    graph.fail_next("PUT", r"/content$", 503, count=2, body={"error": {"code": "custom", "message": "m"}})
    assert api.put(_put_url(graph, p["id"], "x.txt"), data=b"abc").json()["error"]["code"] == "custom"
    assert api.put(_put_url(graph, p["id"], "x.txt"), data=b"abc").status_code == 503
    assert api.put(_put_url(graph, p["id"], "x.txt"), data=b"abc").status_code == 201
    assert graph.pending_faults() == []

    # non-matching method / path are untouched
    graph.fail_next("POST", r"/content$", 500)
    assert api.put(_put_url(graph, p["id"], "y.txt"), data=b"abc").status_code == 201
    graph.clear_faults()

    # early failure without reading the body first still yields a response
    big = os.urandom(3 * 1024 * 1024)
    graph.fail_next("PUT", r"/content$", 503, after_body=False, headers={"Retry-After": "1"})
    r = api.put(_put_url(graph, p["id"], "big.bin"), data=big)
    assert r.status_code == 503 and r.headers["Retry-After"] == "1"
    assert api.put(_put_url(graph, p["id"], "big.bin"), data=big).status_code == 201

    # upload URLs can be failed too (e.g. 500 on a fragment)
    url = _new_session(graph, api, p["id"], "s.bin")["uploadUrl"]
    graph.fail_next("PUT", r"^/upload/", 500)
    assert _put_fragment(url, b"abc", 0, 3).status_code == 500
    assert requests.get(url).json()["nextExpectedRanges"] == ["0-"]
    assert _put_fragment(url, b"abc", 0, 3).status_code == 201


def test_delay_next_commits_before_reply(graph, api):
    p = graph.add_folder("d")
    graph.delay_next("PUT", r"/content$", 1.5)
    t0 = time.monotonic()
    with pytest.raises(requests.exceptions.ReadTimeout):
        api.put(_put_url(graph, p["id"], "slow.txt"), data=b"payload", timeout=(5, 0.3))
    assert time.monotonic() - t0 < 1.4
    assert graph.tree()["d/slow.txt"] == b"payload"  # committed even though the reply was lost
    assert graph.requests_log[-1]["fault"] == "delay" and graph.requests_log[-1]["status"] == 201
    # server keeps working (a new connection is used by the session)
    r = api.get(graph.base_url + "/me", timeout=5)
    assert r.status_code == 200

    # upload-session fragment variant
    url = _new_session(graph, api, p["id"], "s.bin")["uploadUrl"]
    graph.delay_next("PUT", r"^/upload/", 1.0)
    with pytest.raises(requests.exceptions.ReadTimeout):
        _put_fragment(url, b"x" * FRAGMENT_MULTIPLE, 0, FRAGMENT_MULTIPLE + 1, timeout=(5, 0.3))
    st = requests.get(url, timeout=5).json()
    assert st["nextExpectedRanges"] == ["%d-" % FRAGMENT_MULTIPLE]


def test_drop_next(graph, api):
    p = graph.add_folder("d")
    graph.drop_next("PUT", r"/content$")
    with pytest.raises(requests.exceptions.ConnectionError):
        api.put(_put_url(graph, p["id"], "dropped.txt"), data=b"abc", timeout=5)
    assert "d/dropped.txt" not in graph.tree()
    assert graph.requests_log[-1]["status"] is None and graph.requests_log[-1]["fault"] == "drop"
    assert api.put(_put_url(graph, p["id"], "dropped.txt"), data=b"abc", timeout=5).status_code == 201

    graph.drop_next("PUT", r"/content$", process=True)
    with pytest.raises(requests.exceptions.ConnectionError):
        api.put(_put_url(graph, p["id"], "lost-reply.txt"), data=b"xyz", timeout=5)
    assert graph.tree()["d/lost-reply.txt"] == b"xyz"


def test_expire_tokens(graph, api):
    assert api.get(graph.base_url + "/me").status_code == 200
    p = graph.add_folder("e")
    url = _new_session(graph, api, p["id"], "f.bin")["uploadUrl"]
    graph.expire_tokens()
    r = api.get(graph.base_url + "/me")
    assert r.status_code == 401 and _err_code(r) == "InvalidAuthenticationToken"
    assert "WWW-Authenticate" in r.headers
    # upload URLs are pre-authenticated and keep working
    assert _put_fragment(url, b"abc", 0, 3).status_code == 201
    graph.set_valid_tokens({"fresh"})
    assert api.get(graph.base_url + "/me").status_code == 401
    assert requests.get(graph.base_url + "/me", headers={"Authorization": "Bearer fresh"}).status_code == 200
    graph.set_valid_tokens({TOKEN})
    assert api.get(graph.base_url + "/me").status_code == 200


# --------------------------------------------------------------------------
# HTTP robustness
# --------------------------------------------------------------------------


def test_keep_alive_and_content_length(graph):
    f = graph.add_file("k/f.txt", b"abc")
    u = urlsplit(graph.base_url)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
    try:
        conn.request("GET", "/v1.0/me", headers=AUTH)
        r1 = conn.getresponse()
        assert r1.status == 200 and r1.getheader("Content-Length") is not None
        r1.read()
        sock1 = conn.sock
        conn.request("GET", "/v1.0/nope", headers=AUTH)
        r2 = conn.getresponse()
        assert r2.status == 400 and r2.getheader("Content-Length") is not None
        r2.read()
        conn.request("DELETE", "/v1.0/drives/%s/items/%s" % (graph.drive_id, f["id"]), headers=AUTH)
        r3 = conn.getresponse()
        assert r3.status == 204
        r3.read()
        conn.request("PUT", "/v1.0/me/drive/root:/k/g.txt:/content", body=b"x" * 1000, headers=AUTH)
        r4 = conn.getresponse()
        assert r4.status == 201
        r4.read()
        conn.request("GET", "/v1.0/me", headers={"Authorization": "Bearer bad"})
        r5 = conn.getresponse()
        assert r5.status == 401
        r5.read()
        assert conn.sock is sock1  # all on one persistent connection
    finally:
        conn.close()


def test_survives_client_disconnects(graph, api):
    u = urlsplit(graph.base_url)
    for _ in range(3):
        s = socket.create_connection((u.hostname, u.port), timeout=5)
        s.sendall(b"PUT /v1.0/me/drive/root:/x.bin:/content HTTP/1.1\r\nHost: x\r\n"
                  b"Authorization: Bearer test-token\r\nContent-Length: 100000\r\n\r\nshort")
        s.close()
        s = socket.create_connection((u.hostname, u.port), timeout=5)
        s.sendall(b"GET /v1.0/me HTTP/1.1\r\nHost: x\r\n")  # incomplete headers
        s.close()
    # a delayed reply to a client that already left must not break anything
    graph.delay_next("GET", r"/me/drive$", 0.3)
    with pytest.raises(requests.exceptions.ReadTimeout):
        api.get(graph.base_url + "/me/drive", timeout=(5, 0.05))
    time.sleep(0.5)
    assert api.get(graph.base_url + "/me", timeout=5).status_code == 200
    assert "x.bin" not in graph.tree()


def test_malformed_and_unsupported_requests_get_json_errors(graph):
    u = urlsplit(graph.base_url)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
    try:
        conn.request("MOVE", "/v1.0/me", headers=AUTH)
        r = conn.getresponse()
        assert r.status == 501
        assert r.getheader("Content-Type").startswith("application/json")
        assert "error" in __import__("json").loads(r.read())
    finally:
        conn.close()
    s = socket.create_connection((u.hostname, u.port), timeout=5)
    try:
        s.sendall(b"GARBAGE\r\n\r\n")
        data = s.recv(65536)
        assert data.startswith(b"HTTP/") and b"Content-Length" in data
    finally:
        s.close()
    assert requests.get(graph.base_url + "/me", headers=AUTH, timeout=5).status_code == 200


def test_requests_log_shape(graph, api):
    api.get(graph.base_url + "/me/drive", params={"$select": "id"})
    e = graph.requests_log[-1]
    assert {"method", "path", "query", "headers", "status"} <= set(e)
    assert e["method"] == "GET" and e["path"] == "/v1.0/me/drive" and e["status"] == 200
    assert "select" in e["query"]
    assert e["headers"]["authorization"] == "Bearer " + TOKEN


def test_concurrent_requests(graph, api):
    import concurrent.futures

    p = graph.add_folder("c")

    def up(i):
        with requests.Session() as s:
            s.headers.update(AUTH)
            return s.put(_put_url(graph, p["id"], "f%d.txt" % i), data=b"%d" % i, timeout=10).status_code

    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(up, range(40)))
    assert codes == [201] * 40
    assert len(graph.tree()) == 40
