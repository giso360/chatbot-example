# One-time setup for the GitHub Actions deployment

`.github/workflows/deploy.yml` runs on every push to `master`: build image -> push to ECR ->
SSH to EC2 -> swap the container (with health check and automatic rollback).
It opens SSH (port 22) in the security group **only for the runner's IP and only for the
duration of the run**, then closes it again.

## 1. AWS: let GitHub assume a role (OIDC, no stored AWS keys)

Create the OIDC provider once per account (skip if it exists):

- Provider URL `https://token.actions.githubusercontent.com`, audience `sts.amazonaws.com`.

Create an IAM role (e.g. `github-deploy-bedrock-chat`).

**Trust policy**:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Federated": "arn:aws:iam::881490130721:oidc-provider/token.actions.githubusercontent.com" },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": { "token.actions.githubusercontent.com:aud": "sts.amazonaws.com" },
      "StringLike":   { "token.actions.githubusercontent.com:sub": "repo:giso360/chatbot-example:ref:refs/heads/master" }
    }
  }]
}
```

**Permissions policy:**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*" },
    {
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"
      ],
      "Resource": "arn:aws:ecr:us-east-1:881490130721:repository/gskoufias-bedrock-chat"
    },
    { "Effect": "Allow", "Action": "ec2:DescribeInstances", "Resource": "*" },
    {
      "Effect": "Allow",
      "Action": ["ec2:AuthorizeSecurityGroupIngress", "ec2:RevokeSecurityGroupIngress"],
      "Resource": "arn:aws:ec2:us-east-1:881490130721:security-group/sg-0ff54723d56f9d1db"
    }
  ]
}
```

## 2. GitHub repository secrets (Settings -> Secrets and variables -> Actions)

| Secret | Value |
|---|---|
| `AWS_ROLE_ARN` | ARN of the role above |
| `EC2_SSH_PRIVATE_KEY` | full contents of `key-georges.pem` (including the BEGIN/END lines) |

## 3. Push

```bash
# (this folder is already a repository on branch master)
# remote: https://github.com/giso360/chatbot-example
git add . && git commit -m "Add CI/CD"
git push origin master      # triggers the first deployment
```

## Notes

- Rollback: if the new container is not healthy within ~90 s, the script restores the previous
  one and the workflow fails. A manual re-run is available from the Actions tab
  (`workflow_dispatch`).
- Each image is also tagged with the commit SHA, so you can redeploy an old version.
- The first SSH connection to the instance trusts its host key on first use (`accept-new`).
