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

API Dock supports encrypting sensitive authentication tokens using multiple encryption methods:

1. **Local Key Encryption** - Uses a local key file (default)
2. **Environment Variable Encryption** - Uses a key from environment variables
3. **AWS KMS Encryption** - Uses AWS Key Management Service
4. **GCP Secrets** - For retrieval only (not encryption)

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
  method: "list"
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
  method: "list"
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

### AWS KMS Encryption

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

## Configuration Examples

### Simple Local Encryption
```yaml
authentication:
  key: "auth_token"
  method: "fixed"
  value: "gAAAAABh7J8K3...encrypted..."
  encrypted: true
```

### List with Mixed Encryption
```yaml
authentication:
  key: "auth_token"
  method: "list"
  values:
    - value: "gAAAAABh7J8K3...encrypted..."
      encrypted: true
    - value: "plaintext-token"
      encrypted: false
  encrypted: true  # Default for values without explicit setting
  encryption:
    method: "local_key"
```

### AWS KMS with Custom Response
```yaml
authentication:
  key: "X-API-Key"
  method: "fixed"
  value: "AQICAHh7...kms_encrypted..."
  encrypted: true
  encryption:
    method: "aws_kms"
    key_id: "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789012"
    region: "us-east-1"
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