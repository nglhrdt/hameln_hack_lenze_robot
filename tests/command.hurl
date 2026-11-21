POST {{base_url}}/command
Content-Type: application/json
{"inputType": "text", "data": "Please move right"}
HTTP 200
[Asserts]
jsonpath "$.result" == "command exectued"
