import urllib.request
import json
import pprint

# トークン発行
obj = { 'APIPassword': 'xxxxxx' }
json_data = json.dumps(obj).encode('utf8')

url = 'http://localhost:18080/kabusapi/token'
req = urllib.request.Request(url, json_data, method='POST')
req.add_header('Content-Type', 'application/json')

token = None
try:
    with urllib.request.urlopen(req) as res:
        content = json.loads(res.read())
        token = content.get('Token')
        print("トークンの発行に成功しました")
        print(token)
except urllib.error.HTTPError as e:
    print(e)
    content = json.loads(e.read())
    pprint.pprint(content)
    exit(1)
except Exception as e:
    print(e)
    exit(1)

if not token:
    print("トークンの取得に失敗しました")
    exit(1)

# 発注
# order_obj = { 
#     'Symbol': '9433',
#     'Exchange': 1,
#     'SecurityType': 1,
#     'Side': '2',
#     'CashMargin': 1,
#     'DelivType': 2,
#     'FundType': 'AA',
#     'AccountType': 2,
#     'Qty': 100,
#     'FrontOrderType': 30,
#     'Price': 2762.5,
#     'ExpireDay': 0,
#     'ReverseLimitOrder': {
#         'TriggerSec': 3,
#         'TriggerPrice': 1600,
#         'UnderOver': 2,
#         'AfterHitOrderType': 1,
#         'AfterHitPrice': 0
#     }
# }
# order_json_data = json.dumps(order_obj).encode('utf-8')

# order_url = 'http://localhost:18080/kabusapi/sendorder'
# order_req = urllib.request.Request(order_url, order_json_data, method='POST')
# order_req.add_header('Content-Type', 'application/json')
# order_req.add_header('X-API-KEY', token)

# try:
#     with urllib.request.urlopen(order_req) as res:
#         print("\n発注結果:")
#         print(res.status, res.reason)
#         for header in res.getheaders():
#             print(header)
#         print()
#         content = json.loads(res.read())
#         pprint.pprint(content)
# except urllib.error.HTTPError as e:
#     print(e)
#     content = json.loads(e.read())
#     pprint.pprint(content)
# except Exception as e:
#     print(e)
