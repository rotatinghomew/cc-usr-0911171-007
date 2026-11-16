人才认定证据版本与复核申诉注册库
测试：python3 -m unittest discover -s tests -v
构建：python3 -m compileall -q talent_credentials tests
验收：python3 -m talent_credentials --database /tmp/talent_credentials.sqlite3 run --fixture fixtures/acceptance.json --output /tmp/talent_credentials-output.json
单测：python3 -m unittest tests.test_domain -v
