# Deployment setup (no secrets)

`.github/workflows/deploy.yml` runs on every push to `master` on a **self-hosted GitHub Actions
runner installed on the EC2 instance itself**. It checks out the code, builds the image locally
and swaps the container (`scripts/deploy-local.sh`), with a health check and automatic rollback.

Because the runner already lives on the server:

- no AWS keys are stored anywhere (Bedrock access comes from the instance's IAM role),
- no SSH key is needed and no security group rule has to be opened for GitHub,
- no ECR push is needed (the image is built where it runs).

## One-time runner registration (on the instance)

GitHub gives a short-lived registration token (valid 1 hour, single use; it is not a stored
secret): repository -> Settings -> Actions -> Runners -> **New self-hosted runner** (Linux, x64)
and copy the token from the `./config.sh --token ...` line. Then, on the instance:

```bash
cd ~/actions-runner
./config.sh --unattended --url https://github.com/giso360/chatbot-example --token <TOKEN> \
            --labels bedrock-chat --name ec2-bedrock-chat --replace
sudo ./svc.sh install ubuntu && sudo ./svc.sh start     # keeps it running across reboots
```

The runner user (`ubuntu`) needs passwordless `sudo` for docker, which this instance already has.

## Security notes

- A self-hosted runner executes workflow code **on your server**. The workflow only triggers on
  pushes to `master` and manual runs, never on pull requests. Keep it that way, and since the
  repository is public, leave Settings -> Actions -> General -> *Fork pull request workflows*
  on "Require approval".
- Anyone who can push to `master` can run code on the instance. Protect the branch.
- The instance is small (about 900 MB RAM): the build briefly competes with the running app.
  Adding a 1 GB swap file avoids out-of-memory kills during builds.
