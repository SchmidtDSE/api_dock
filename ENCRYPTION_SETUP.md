```
  1. AWS Setup Steps: See the detailed steps in the documentation above, but essentially:
    - Create KMS key in AWS Console or CLI
    - Configure AWS credentials (aws configure or environment variables)
    - Use api-dock encrypt --method aws_kms --key-id "your-kms-arn" "value"
  2. Getting Encrypted Values: Use the CLI commands:
    - api-dock generate-key (for local encryption)
    - api-dock encrypt "your-value" (for each authentication token)
  3. Documentation Location: I just created ENCRYPTION_SETUP.md with comprehensive instructions covering all encryption methods, CLI commands, configuration examples, and troubleshooting.

  The fastest solution for your current error is Option 1 (local key encryption) since it doesn't require AWS setup.
```

# API Dock Encryption Setup Guide

This guide explains how to set up encryption for authentication values in API Dock.

## Overview

API Dock supports multiple authentication methods and encryption options for securing sensitive tokens:

### Authentication Methods
1. **[Fixed Value](#1-fixed-value-value)** - Single authentication token
2. **[List of Values](#2-list-of-values-values)** - Multiple allowed tokens
3. **[File-based](#3-file-based-filepath)** - Tokens from a text file (one per line)
4. **[AWS Secrets Manager](#4a-aws-secrets-manager-aws_secret_name)** - Tokens stored as plaintext in AWS Secrets Manager
5. **[AWS KMS](#4b-aws-kms-aws_key_id)** - Tokens encrypted with AWS KMS (inline or file-based)
6. **[GCP Secret Manager](#5-gcp-secret-manager-gcp_project_id)** - Tokens stored in Google Cloud Secret Manager

### Encryption Methods (for encrypting individual values)
1. **[Local Key Encryption](#local-key-encryption-default)** - Uses a local key file (default)
2. **[Environment Variable Encryption](#environment-variable-encryption)** - Uses a key from environment variables
3. **[AWS KMS Encryption](#aws-kms-encryption-for-encryptingdecrypting-values)** - Uses AWS Key Management Service for individual values

## Quick Start

### 1. Generate an Encryption Key

First, generate an encryption key:

```bash
api-dock generate-key
```

This creates a `.api_dock_key` file in your current directory with proper permissions (600).

### 2. Encrypt Your Authentication Values

Encrypt each authentication value:

```bash
# Encrypt your authentication tokens
api-dock encrypt "546"
api-dock encrypt "9887"
api-dock encrypt "123"
```

Example output:
```
Encrypted value: gAAAAABh7J8K3...encoded_value_1...
Encrypted value: gAAAAABh7J8K4...encoded_value_2...
Encrypted value: gAAAAABh7J8K5...encoded_value_3...
```

### 3. Update Your Configuration

Replace the plaintext values in your config with the encrypted ones:

```yaml
authentication:
  key: "a"
  values:
    - "gAAAAABh7J8K3...encoded_value_1..."
    - "gAAAAABh7J8K4...encoded_value_2..."
    - "gAAAAABh7J8K5...encoded_value_3..."
  encrypted: true
  failed_response:
    status: 401
    message: "Access denied 2"
```

### 4. Configure Encryption Method (Optional)

Add encryption configuration to specify how to decrypt:

```yaml
authentication:
  key: "a"
  values: ["gAAAAABh..."]
  encrypted: true
  encryption:
    method: "local_key"
    key_file: ".api_dock_key"
```

## Encryption Methods

### Local Key Encryption (Default)

**Configuration:**
```yaml
encryption:
  method: "local_key"
  key_file: ".api_dock_key"  # Optional, defaults to .api_dock_key
```

**Setup:**
1. Generate key: `api-dock generate-key`
2. Encrypt values: `api-dock encrypt "your-token"`
3. Use encrypted values in config

### Environment Variable Encryption

**Configuration:**
```yaml
encryption:
  method: "env_key"
  key_env: "API_DOCK_ENCRYPTION_KEY"  # Optional, this is the default
```

**Setup:**
1. Generate key: `api-dock generate-key --output my_key`
2. Set environment: `export API_DOCK_ENCRYPTION_KEY=$(cat my_key)`
3. Encrypt: `api-dock encrypt --method env_key "your-token"`
4. Use encrypted values in config

### AWS KMS Encryption (for encrypting/decrypting values)

This section covers using AWS KMS to encrypt/decrypt individual values in your configuration files. This is different from using AWS services for authentication (covered later).

**Configuration:**
```yaml
encryption:
  method: "aws_kms"
  key_id: "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789012"
  region: "us-east-1"  # Optional, defaults to us-east-1
```

**Setup:**

1. **Create KMS Key in AWS:**
   ```bash
   aws kms create-key --description "API Dock encryption key"
   ```

2. **Configure AWS credentials** (one of):
   - AWS CLI: `aws configure`
   - Environment variables: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
   - IAM role (if running on EC2)

3. **Encrypt values:**
   ```bash
   api-dock encrypt --method aws_kms --key-id "arn:aws:kms:..." "your-token"
   ```

4. **Required IAM permissions:**
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": [
           "kms:Encrypt",
           "kms:Decrypt"
         ],
         "Resource": "arn:aws:kms:us-east-1:123456789012:key/your-key-id"
       }
     ]
   }
   ```

> **Note:** This is for encrypting individual values. For AWS-based authentication methods, see the "AWS Authentication" section below.

## CLI Commands

### Generate Key
```bash
api-dock generate-key [options]

Options:
  --output, -o    Output file for the key (default: .api_dock_key)
  --force, -f     Overwrite existing key file
```

### Encrypt Values
```bash
api-dock encrypt [value] [options]

Options:
  --method, -m        Encryption method (local_key|env_key|aws_kms)
  --key-file          Key file path (for local_key)
  --key-env           Environment variable name (for env_key)
  --key-id            AWS KMS key ID (for aws_kms)
  --region            AWS region (for aws_kms)
```

### Decrypt Values (for testing)
```bash
api-dock decrypt [encrypted_value] [options]
```

## Authentication Methods

The authentication method is automatically inferred from the configuration keys you provide. You must specify exactly one of the following:

### 1. Fixed Value (`value`)
Single authentication token:
```yaml
authentication:
  key: "auth_token"
  value: "gAAAAABh7J8K3...encrypted..."
  encrypted: true
```

### 2. List of Values (`values`)
Multiple allowed tokens:
```yaml
authentication:
  key: "auth_token"
  values:
    - "gAAAAABh7J8K3...encrypted_1..."
    - "gAAAAABh7J8K4...encrypted_2..."
    - "gAAAAABh7J8K5...encrypted_3..."
  encrypted: true
```

### 3. File-based (`filepath`)
Tokens from a text file (one per line):
```yaml
authentication:
  key: "auth_token"
  filepath: "/path/to/tokens.txt"
  encrypted: true  # Whether tokens in file are encrypted
```

**Setting up file-based authentication:**

1. **Create a tokens file:**
   ```bash
   # Create your tokens file
   touch /path/to/tokens.txt
   chmod 600 /path/to/tokens.txt  # Secure permissions
   ```

2. **Add tokens to file (if using encrypted tokens):**
   ```bash
   # Generate key first
   api-dock generate-key

   # Encrypt each token and add to file
   api-dock encrypt "token1" >> /path/to/tokens.txt
   api-dock encrypt "token2" >> /path/to/tokens.txt
   api-dock encrypt "token3" >> /path/to/tokens.txt
   ```

3. **Or add plaintext tokens:**
   ```bash
   echo "plaintext-token-1" >> /path/to/tokens.txt
   echo "plaintext-token-2" >> /path/to/tokens.txt
   # Set encrypted: false in config
   ```

Example tokens.txt:
```
# Lines starting with # are comments
gAAAAABh7J8K3...encrypted_token_1...
gAAAAABh7J8K4...encrypted_token_2...
# Empty lines are ignored

gAAAAABh7J8K5...encrypted_token_3...
```

**File format rules:**
- One token per line
- Lines starting with `#` are treated as comments
- Empty lines are ignored
- Whitespace at beginning/end of lines is trimmed

### 4. AWS Authentication

There are two methods for authenticating using AWS services:

- **AWS Secrets Manager**: Tokens stored as plaintext in AWS Secrets Manager (AWS handles encryption automatically)
- **AWS KMS**: Tokens encrypted with AWS KMS and stored in your config file (you handle encryption/decryption)

#### 4a. AWS Secrets Manager (`aws_secret_name`)
Authentication tokens stored in AWS Secrets Manager:

```yaml
authentication:
  key: "X-API-Key"
  aws_secret_name: "my-app/api-tokens"
  aws_region: "us-west-2"  # Optional, defaults to us-west-2
  refresh_interval: 30    # Cache TTL in seconds
  failed_response:
    status: 403
    error: "Invalid API key"
```

**Setup:**
1. **Create secret in AWS Secrets Manager:**
   ```bash
   # Single token
   aws secretsmanager create-secret --region us-west-2 --name "my-app/api-tokens" --secret-string "your-secret-token"

   # Multiple tokens (JSON list)
   aws secretsmanager create-secret --region us-west-2 --name "my-app/api-tokens" --secret-string '["token1", "token2", "token3"]'

   # Or use a different region if specified in your config
   aws secretsmanager create-secret --region us-east-1 --name "my-app/api-tokens" --secret-string "your-secret-token"
   ```

2. **Required IAM permissions:**

   **⚠️ Note for AWS SSO Users:** If you're using AWS SSO with an admin role (like `UCB-FederatedAdmins`), you likely already have the necessary permissions and can **skip this entire step**. Test by trying to access your secret first:
   ```bash
   aws secretsmanager get-secret-value --region us-west-2 --secret-id "your-secret-name"
   ```

   **Add through AWS Console:**
   Go to [IAM Policies](https://console.aws.amazon.com/iam/home#/policies) → Create Policy → JSON tab → paste the policy below (replace the ARN with your secret's ARN)

   **Or add using AWS CLI:**

   **Step 1: Create the policy (do this once):**
   ```bash
   # Create the policy (replace the ARN with your secret's ARN)
   aws iam create-policy --policy-name "ApiDockSecretsAccess" --policy-document '{
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": ["secretsmanager:GetSecretValue"],
         "Resource": "arn:aws:secretsmanager:us-west-2:123456789012:secret:my-app/api-tokens-*"
       }
     ]
   }'
   ```

   **Step 2: Attach to user/role (choose one option):**

   **For IAM Users:**
   ```bash
   # Find your username first
   aws iam get-user --query 'User.UserName' --output text

   # Attach to your user (replace USER_NAME and ACCOUNT_ID)
   aws iam attach-user-policy --user-name USER_NAME --policy-arn "arn:aws:iam::ACCOUNT_ID:policy/ApiDockSecretsAccess"
   ```

   **For AWS SSO (Federated) Users:**
   ```bash
   # Check your current identity (you'll see a federated role ARN)
   aws sts get-caller-identity
   ```

   **⚠️ Important:** AWS SSO roles are protected and cannot be modified directly. You'll get an "UnmodifiableEntity" error if you try to attach policies to them.

   **Solutions for SSO users:**
   1. **Check existing permissions:** Your SSO role likely already has the necessary permissions
   2. **Use AWS Console:** Go to [IAM Policies](https://console.aws.amazon.com/iam/home#/policies) (much easier)
   3. **Contact your AWS admin:** Ask them to add the permissions through AWS SSO permission sets
   4. **Create a custom IAM user:** For dedicated API Dock access (see "Different IAM User" section below)

   **For Attaching to a Different IAM User:**
   ```bash
   # List existing IAM users to find the target user
   aws iam list-users --query 'Users[*].UserName' --output table

   # Attach to any IAM user (replace TARGET_USER_NAME and ACCOUNT_ID)
   aws iam attach-user-policy --user-name TARGET_USER_NAME --policy-arn "arn:aws:iam::ACCOUNT_ID:policy/ApiDockSecretsAccess"
   ```

   **For Attaching to an IAM Role:**
   ```bash
   # List existing roles to find the target role
   aws iam list-roles --query 'Roles[*].RoleName' --output table

   # Attach to any IAM role (replace TARGET_ROLE_NAME and ACCOUNT_ID)
   aws iam attach-role-policy --role-name TARGET_ROLE_NAME --policy-arn "arn:aws:iam::ACCOUNT_ID:policy/ApiDockSecretsAccess"
   ```

   **Note:** If `aws iam get-user` returns "Must specify userName when calling with non-User credentials", you're using AWS SSO/federated credentials and should use the role-based approach. SSO users often already have the necessary permissions through their federated role.

   **Policy JSON:**
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": [
           "secretsmanager:GetSecretValue"
         ],
         "Resource": "arn:aws:secretsmanager:us-west-2:123456789012:secret:my-app/api-tokens-*"
       }
     ]
   }
   ```

   **Note:** Replace `us-west-2`, `123456789012`, and `my-app/api-tokens` with your actual region, AWS account ID, and secret name.

#### 4b. AWS KMS (`aws_key_id`)
Authentication tokens encrypted with AWS KMS:

**Option 1: Inline tokens**
```yaml
authentication:
  key: "X-API-Key"
  aws_key_id: "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789012"
  aws_tokens:
    - "AQICAHh7...kms_encrypted_token_1..."
    - "AQICAHh7...kms_encrypted_token_2..."
    - "AQICAHh7...kms_encrypted_token_3..."
  aws_region: "us-west-2"  # Optional, defaults to us-west-2
  failed_response:
    status: 403
    error: "Invalid API key"
```

**Option 2: File-based tokens**
```yaml
authentication:
  key: "X-API-Key"
  aws_key_id: "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789012"
  aws_tokens_file: "/path/to/kms_encrypted_tokens.txt"
  aws_region: "us-west-2"  # Optional, defaults to us-west-2
  failed_response:
    status: 403
    error: "Invalid API key"
```

**Setup:**
1. **Create KMS key:**
   ```bash
   aws kms create-key --description "API Dock authentication tokens"
   ```

2. **Encrypt your tokens:**
   ```bash
   # Encrypt each authentication token
   api-dock encrypt --method aws_kms --key-id "arn:aws:kms:..." "token1"
   api-dock encrypt --method aws_kms --key-id "arn:aws:kms:..." "token2"
   api-dock encrypt --method aws_kms --key-id "arn:aws:kms:..." "token3"
   ```

3. **For file-based tokens, create tokens file:**
   ```bash
   # Create file with KMS-encrypted tokens (one per line)
   touch /path/to/kms_encrypted_tokens.txt
   chmod 600 /path/to/kms_encrypted_tokens.txt

   # Add encrypted tokens to file
   api-dock encrypt --method aws_kms --key-id "arn:aws:kms:..." "token1" >> /path/to/kms_encrypted_tokens.txt
   api-dock encrypt --method aws_kms --key-id "arn:aws:kms:..." "token2" >> /path/to/kms_encrypted_tokens.txt
   ```

   Example kms_encrypted_tokens.txt:
   ```
   # Lines starting with # are comments
   AQICAHh7...kms_encrypted_token_1...
   AQICAHh7...kms_encrypted_token_2...
   # Empty lines are ignored

   AQICAHh7...kms_encrypted_token_3...
   ```

4. **Required IAM permissions:**
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": [
           "kms:Decrypt"
         ],
         "Resource": "arn:aws:kms:us-east-1:123456789012:key/your-key-id"
       }
     ]
   }
   ```

### 5. GCP Secret Manager (`gcp_project_id`)
Tokens stored in Google Cloud Secret Manager:
```yaml
authentication:
  key: "Authorization"
  gcp_project_id: "my-project-123"
  gcp_secret_name: "api-tokens"
  gcp_version: "latest"    # Optional, defaults to latest
  refresh_interval: 300    # Cache TTL in seconds
```

## Configuration Examples

### List with Mixed Encryption
```yaml
authentication:
  key: "auth_token"
  values:
    - value: "gAAAAABh7J8K3...encrypted..."
      encrypted: true
    - value: "plaintext-token"
      encrypted: false
  encrypted: true  # Default for values without explicit setting
  encryption:
    method: "local_key"
```

### File-based Authentication
```yaml
authentication:
  key: "auth_token"
  filepath: "auth_tokens.txt"
  encrypted: true
  encryption:
    method: "local_key"
    key_file: ".api_dock_key"
  failed_response:
    status: 401
    message: "Invalid authentication token"
```

### AWS KMS Authentication
```yaml
authentication:
  key: "X-API-Key"
  aws_key_id: "arn:aws:kms:us-west-2:123456789012:key/12345678-1234-1234-1234-123456789012"
  aws_tokens:
    - "AQICAHh7J8K3LxYrVhw1e..."  # KMS-encrypted "token123"
    - "AQICAHh7J8K4LxYrVhw2f..."  # KMS-encrypted "secret456"
    - "AQICAHh7J8K5LxYrVhw3g..."  # KMS-encrypted "auth789"
  aws_region: "us-west-2"
  failed_response:
    status: 403
    error: "Access denied"
```

### AWS Secrets Manager with Local Encryption
```yaml
authentication:
  key: "X-API-Key"
  aws_secret_name: "my-app/encrypted-tokens"
  aws_region: "us-west-2"
  # Tokens in AWS secret are encrypted with local key
  encrypted: true
  encryption:
    method: "local_key"
    key_file: "/secure/path/.api_dock_key"
  failed_response:
    status: 403
    error: "Invalid API key"
    code: "AUTH_FAILED"
```

## Troubleshooting

### "cryptography package is required"
Install the cryptography package:
```bash
pixi add cryptography
# or
pip install cryptography
```

### "Encryption key not found"
- Check that `.api_dock_key` exists in your working directory
- Or set `API_DOCK_ENCRYPTION_KEY` environment variable
- Or specify custom path in `encryption.key_file`

### "Invalid encryption key"
- Key file may be corrupted
- Generate a new key: `api-dock generate-key --force`

### "AWS credentials not found"
- Configure AWS CLI: `aws configure`
- Or set environment variables: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
- Or use IAM roles if running on AWS

### "Failed to decrypt authentication value"
- Ensure the encrypted value was created with the same key/method
- Check that encryption configuration matches the method used to encrypt

## Security Best Practices

1. **Key File Permissions**: Keep `.api_dock_key` with 600 permissions (owner read/write only)
2. **Environment Variables**: Use secure methods to set encryption keys in production
3. **AWS KMS**: Use IAM policies to restrict access to encryption keys
4. **Key Rotation**: Periodically rotate encryption keys and re-encrypt values
5. **Backup**: Safely backup encryption keys (but not in your code repository)

## Integration with CI/CD

### GitHub Actions Example
```yaml
env:
  API_DOCK_ENCRYPTION_KEY: ${{ secrets.API_DOCK_KEY }}
```

### Docker Example
```dockerfile
# Copy key file
COPY --chmod=600 .api_dock_key /app/.api_dock_key

# Or use environment variable
ENV API_DOCK_ENCRYPTION_KEY="base64_encoded_key"
```