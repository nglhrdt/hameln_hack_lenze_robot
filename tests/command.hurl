POST {{base_url}}/command
Content-Type: application/json
{"inputType": "text", "data": "Please go home"}
HTTP 200
[Asserts]
jsonpath "$.result" == "command exectued"
