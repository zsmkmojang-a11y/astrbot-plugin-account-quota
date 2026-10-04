"""测试用 stdio 服务器，只检查握手和查询，不调用任何模型。"""
import json
import sys
import time

mode = sys.argv[1]
initialized = False
for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    if method == "initialize":
        assert request["params"]["clientInfo"]["name"] == "astrbot_account_quota"
        print(json.dumps({"id": request["id"], "result": {}}), flush=True)
    elif method == "initialized":
        initialized = True
    elif method == "account/rateLimits/read":
        assert initialized
        if mode == "hang":
            time.sleep(60)
        elif mode == "error":
            print(json.dumps({"id": request["id"], "error": {"code": 401, "message": "SECRET_TOKEN"}}), flush=True)
        elif mode == "invalid":
            print("invalid json", flush=True)
        elif mode == "eof":
            break
        else:
            sys.stderr.write("private diagnostics" * 10000)
            sys.stderr.flush()
            print(json.dumps({"method": "account/rateLimits/updated", "params": {}}), flush=True)
            print(json.dumps({"id": request["id"], "result": {"rateLimits": {"primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1800000000}}}}), flush=True)
    else:
        raise AssertionError("Unexpected RPC")
