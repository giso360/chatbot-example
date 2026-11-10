# Deploying Bedrock Chat to EC2 with Docker

## 1. Credentials: use an IAM role, don't copy keys

You do **not** need to bring your local credentials to EC2. boto3 finds credentials automatically,
and on EC2 it gets temporary ones from the instance's IAM role through the metadata service (IMDS).
No keys go into the image, the compose file or the instance.

### Create the role (once)

`bedrock-chat-policy.json` — allows only what the app calls (`converse` / `converse_stream`):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    "Resource": "*"
  }]
}
```

`Resource: "*"` is a starting point. The model id is a *global inference profile*, which is
evaluated against both the profile ARN and the underlying model ARNs in several regions, so
tighten it only after testing (check the CloudTrail / AccessDenied message for the ARNs it needs).

```bash
aws iam create-policy --policy-name BedrockChatInvoke --policy-document file://bedrock-chat-policy.json

aws iam create-role --role-name bedrock-chat-ec2 --assume-role-policy-document \
  '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

aws iam attach-role-policy --role-name bedrock-chat-ec2 --policy-arn arn:aws:iam::<ACCOUNT_ID>:policy/BedrockChatInvoke
# only needed if you pull the image from ECR:
aws iam attach-role-policy --role-name bedrock-chat-ec2 --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly

aws iam create-instance-profile --instance-profile-name bedrock-chat-ec2
aws iam add-role-to-instance-profile --instance-profile-name bedrock-chat-ec2 --role-name bedrock-chat-ec2
aws ec2 associate-iam-instance-profile --instance-id <INSTANCE_ID> --iam-instance-profile Name=bedrock-chat-ec2
```

Also make sure the model is enabled for your account in the Bedrock console.

### IMDS hop limit (important for containers)

IMDSv2 answers only 1 network hop by default, and a container on Docker's bridge network is 2 hops
away, so boto3 inside the container would find **no credentials**. Raise the limit:

```bash
aws ec2 modify-instance-metadata-options --instance-id <INSTANCE_ID> \
  --http-put-response-hop-limit 2 --http-tokens required
```

(Alternative: run the container with `--network host`.)

## 2. Build and ship the image

```bash
docker build -t bedrock-chat:latest .
```

The image must match the instance CPU: use `--platform linux/amd64` for x86 instances (the default
on a Windows/Intel PC) or `--platform linux/arm64` for Graviton instances.

**Via ECR (recommended):**

```bash
aws ecr create-repository --repository-name bedrock-chat --region us-east-1
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com
docker tag bedrock-chat:latest <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/bedrock-chat:latest
docker push <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/bedrock-chat:latest
```

**Without ECR:** `docker save bedrock-chat:latest | gzip > bedrock-chat.tar.gz`, `scp` it to the
instance, then `gunzip -c bedrock-chat.tar.gz | docker load`.

## 3. Run on the instance (Amazon Linux 2023)

```bash
sudo dnf install -y docker && sudo systemctl enable --now docker && sudo usermod -aG docker ec2-user
# log out and back in, then (ECR only) log in to the registry as in step 2 and:

docker run -d --name bedrock-chat --restart unless-stopped \
  -p 80:8000 \
  -v chat-data:/data \
  -e AWS_REGION=us-east-1 \
  <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/bedrock-chat:latest
```

- Chat history lives in the `chat-data` volume (SQLite at `/data/chat.db`); it survives restarts and upgrades.
- To upgrade: `docker pull ...`, `docker rm -f bedrock-chat`, run the same `docker run` again.
- Check it: `curl localhost/health`, `docker ps` (should say `healthy`), `docker logs bedrock-chat`.
- Settings: `AWS_REGION`, `BEDROCK_MODEL_ID`, `SYSTEM_PROMPT`, `MAX_TOKENS`, `TEMPERATURE`.
- Back up the history with: `docker run --rm -v chat-data:/data -v "$PWD":/backup alpine cp /data/chat.db /backup/`.

## 4. Security — read before opening it to the internet

The app has **no login**: anyone who can reach it can spend your Bedrock budget and read every
saved conversation. Before exposing it:

- Restrict the security group to your own IP / VPN / office range, **or** put it behind an ALB with
  authentication (Cognito/OIDC), **or** add an authentication layer to the app.
- Serve HTTPS (ALB + ACM certificate, or a reverse proxy such as Caddy/nginx) — plain HTTP sends chats in clear text.
- Keep `--http-tokens required` (IMDSv2) as above.

## Testing the image locally with credentials

On your machine there is no instance role. Either mount your AWS config read-only:

```bash
docker run --rm -p 8000:8000 -v chat-data:/data -v "$HOME/.aws:/home/app/.aws:ro" -e AWS_PROFILE=<profile> bedrock-chat:latest
```

or pass temporary credentials from your shell (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_SESSION_TOKEN`) with `-e`. Never bake them into the image or commit them. If your network
re-signs HTTPS traffic (the SSL error seen on the dev machine), the container may need your
corporate CA certificate too; this does not apply on EC2.
