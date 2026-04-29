Borderless rad notification API — three scenarios in order
Audience: n8n workflow developer

Endpoint (same for all three):


PATCH {BASE_URL}/user/radiologist/{rad_id}/borderless
Required headers:


Authorization: <shared-secret>
Content-Type: application/json
QA:

Base URL: https://e2e-qa-api.5cnetwork.com
Authorization: NWNuZXR3b3JrOjVjbmV0d29yaw==

1. Send a BLOCK notification
The rad's app freezes — centered modal on a blurred backdrop, no close button. Rad cannot proceed until ops/n8n clears it.

Use this when n8n decides the rad failed quality / consistency / punctuality / regularity.


curl -X PATCH "https://e2e-qa-api.5cnetwork.com/user/radiologist/2105/borderless" \
  -H "Authorization: NWNuZXR3b3JrOjVjbmV0d29yaw==" \
  -H "Content-Type: application/json" \
  -d '{
    "notice": {
      "kind": "BLOCK",
      "title": "Quality below threshold",
      "body": "Your quality score on the first 20 cases is below the bar required to continue. A team member from Borderless will reach out shortly."
    }
  }'

You write title and body — say whatever the rad needs to see. Use \n for line breaks.

2. Send an INFO notification
The rad sees the same modal layout but with a × close button in the top-right. They can dismiss it themselves and continue using the app.

Use this for non-blocking messages (welcome, qualified, etc).


curl -X PATCH "https://e2e-qa-api.5cnetwork.com/user/radiologist/2105/borderless" \
  -H "Authorization: NWNuZXR3b3JrOjVjbmV0d29yaw==" \
  -H "Content-Type: application/json" \
  -d '{
    "notice": {
      "kind": "INFO",
      "title": "Welcome to Borderless",
      "body": "You have completed incubation. Your earnings dashboard is now live."
    }
  }'
When the rad clicks ×, the frontend automatically calls the clear API for you. Don't try to clear INFO notices yourself — the click handles it.

3. Clear the BLOCK notification
Removes the modal. The rad's app becomes usable again. Use this when ops decides to give the rad another chance after a BLOCK.


curl -X PATCH "https://e2e-qa-api.5cnetwork.com/user/radiologist/2105/borderless" \
  -H "Authorization: NWNuZXR3b3JrOjVjbmV0d29yaw==" \
  -H "Content-Type: application/json" \
  -d '{ "notice": null }'
