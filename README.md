# Post Studio

Turn a topic and a little context into a LinkedIn post and an AI-generated image. Review the draft, edit it yourself or ask the writing assistant for changes, then approve it for a scheduled time. Nothing is published before you approve it.

## What you need

- Python 3.11 or later.
- An API key and model access from the AI provider you choose in Settings. Local Ollama can run without a key. AI requests may use paid credits.
- A [LinkedIn developer app](https://www.linkedin.com/developers/apps) and a personal LinkedIn account. In the app's **Products** tab, enable **Sign In with LinkedIn using OpenID Connect** and **Share on LinkedIn**. Check that `openid`, `profile`, and `w_member_social` are available in the **Auth** tab. LinkedIn may require you to associate and verify a LinkedIn Page when creating the developer app.
- In the LinkedIn app's **Auth** tab, add this exact authorized redirect URL:

  `http://localhost:8000/auth/linkedin/callback`

LinkedIn documents these permissions and its [personal-profile sharing API](https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/share-on-linkedin). This app publishes to your personal profile, not a company page.

## Run locally

Clone the repository and enter its folder:

```bash
git clone https://github.com/Arif2232/Automate-linkedin.git
cd Automate-linkedin
```

On macOS or Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

On Windows (PowerShell):

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

Open [http://localhost:8000](http://localhost:8000), go to **Settings**, choose your writing and image providers, enter any required keys, and select **Test & save** for each. Then connect LinkedIn using the steps below.

Keep the server running and the computer awake for scheduled publishing. The browser tab may be closed. If the app is stopped when an approved post is due, it will publish shortly after the app starts again. Drafts that were not approved are never sent; if their time has passed, choose a new time before approving. Optional browser review notifications work while the page is open.

For unattended schedules, you can run the same server on an always-on computer and access it through an SSH tunnel, such as `ssh -L 8000:127.0.0.1:8000 user@your-server`. Open `http://localhost:8000` on your own computer while the tunnel is connected; do not expose the server's HTTP port publicly.

## AI providers

For writing and review chat, choose OpenAI, Google Gemini, Anthropic Claude, xAI Grok, Groq, DeepSeek, Mistral, Perplexity, OpenRouter, Together AI, Hugging Face, local Ollama, Azure OpenAI, Alibaba Cloud/Qwen, or a custom OpenAI- or Anthropic-compatible endpoint. OpenRouter can also route to many other model providers. The model ID is editable because availability and names vary by account and change over time. For Azure, enter your deployed model name and the base URL ending in `/openai/v1`; for Qwen, enter the OpenAI-compatible URL for your workspace and region. For Ollama, start the local server and download your chosen model first.

For images, choose **Same as writing provider**, OpenAI, Gemini, xAI, OpenRouter, Hugging Face, Azure OpenAI, another OpenAI-compatible image API, or **No image**. OpenAI, Gemini, xAI, OpenRouter, and Hugging Face have dedicated image-generation and editing integrations; the chosen model must support the requested operation. Other providers can supply images only if they implement the OpenAI-compatible images API and return base64 image data. Some writing providers do not support image generation; in that case **Same as writing provider** produces a text-only draft. You can select a separate image provider at any time or explicitly approve a post without an image.

Provider selections apply to new drafts, review chats, image changes, and future repeats. Keys are stored separately for each provider; the same provider's key is shared between writing and images when applicable. Custom endpoints require HTTPS, except for loopback addresses on the same machine. To avoid sending a saved key to a different endpoint, re-enter it when changing a custom URL. Existing OpenAI keys are preserved but need testing after this update. The default OpenAI model IDs can still be overridden before startup with `OPENAI_TEXT_MODEL` and `OPENAI_IMAGE_MODEL`.

**Test & save** makes a real, small request to the selected writing model. The image test generates a sample image and deletes it afterward; both tests can use paid API credits. A successful test is required before creating new drafts or using that AI connection. Changing its key, model, or endpoint requires testing again; already approved posts can still publish. A successful test confirms access at that moment, not future quota or access to every model feature.

OpenRouter's [image API](https://openrouter.ai/blog/insights/image-generation-models-compared/) supports many models; enter the exact model ID and select one that supports reference images to edit an existing image. With [Hugging Face](https://huggingface.co/docs/inference-providers/tasks/image-to-image), choose a model available through Inference Providers for the task; some models generate images but require switching to an image-to-image model for editing. Direct [Gemini image generation](https://ai.google.dev/gemini-api/docs/generate-content/image-generation) and [xAI image editing](https://docs.x.ai/developers/model-capabilities/images/editing) require models enabled for those operations. An API key or model may require billing or additional verification with its provider.

## Connect LinkedIn

1. Open [LinkedIn Developer Portal](https://www.linkedin.com/developers/apps) and select **Create app**. Fill in the required app details and associate a LinkedIn Page. Create a Page if necessary; LinkedIn may ask you to verify it.
2. On your app's **Products** tab, request **Sign In with LinkedIn using OpenID Connect** and **Share on LinkedIn**. Wait until both are approved. The **Auth** tab should show `openid`, `profile`, and `w_member_social`.
3. On the **Auth** tab, copy the **Client ID** and **Client Secret**. Under **Authorized redirect URLs**, add `http://localhost:8000/auth/linkedin/callback` exactly.
4. In Post Studio **Settings**, paste the ID and secret, save them, and choose **Connect LinkedIn**. Sign in on LinkedIn and approve the permissions. Connecting also checks that the app credentials and redirect URL work.

The **client ID** identifies your developer app. The **client secret** proves the app's identity when it exchanges your LinkedIn approval for an access token. The token lets the app publish on your behalf. You sign in on LinkedIn itself, so Post Studio never asks for your LinkedIn password. Keep the secret private; see LinkedIn's [OAuth guide](https://learn.microsoft.com/en-us/linkedin/shared/authentication/authorization-code-flow) for details.

## Create and review

1. Enter a topic, any context or tone, the first publishing time, and optionally a repeat interval in hours, days, or weeks. Times are shown in your device's time zone.
2. The app creates a draft and, when an image provider is configured, an image. If image generation fails, the text is kept and you can try the image again.
3. Edit the text directly, ask the writing assistant questions or for a rewrite, and use its reply if you like it. You can also describe a change to the image. Review the result and save your changes.
4. Connect LinkedIn and choose **Approve & schedule**. If there is no image, explicitly check **Approve this post without an image** first. The approved content is locked; choose **Move back to review** to edit it before publishing begins.

For a repeating schedule, after each successful publication the app prepares one fresh draft for the next future slot. Each draft needs its own approval. Missed repeat slots are skipped instead of producing a backlog. Hourly intervals use elapsed hours; daily and weekly intervals keep the scheduled local time across daylight-saving changes. You can stop repeating without deleting the current draft.

If a publishing attempt fails or the app is interrupted during publishing, check your LinkedIn profile before returning the draft to review. LinkedIn may have accepted a post even if the app did not receive the response; the app will not automatically retry an uncertain publication. Reconnecting a different account or changing LinkedIn app credentials moves approved drafts back to review.

## Privacy and operation

This is a **single-user local app**, not a public web service. It listens only on `127.0.0.1`, checks the request origin, and allows one running instance per data directory. Do not expose it directly to the internet.

Drafts, images, AI provider keys, the LinkedIn client secret, and the LinkedIn access token are stored **unencrypted** in the ignored `data/` directory. On macOS and Linux, the directory and database must be private (`700` and `600`). Keep backups private and use your operating system's account and disk protections; on Windows, use appropriate folder permissions. The browser receives connection status, never the saved secrets. Disconnecting LinkedIn deletes the local token and returns scheduled posts to review. LinkedIn access tokens expire, so reconnect when prompted.

## Tests

On macOS or Linux:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

On Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

The tests use simulated AI and LinkedIn responses; they do not publish to LinkedIn or incur API charges.
