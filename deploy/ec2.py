"""Launch Interlock on one EC2 instance: Docker, FalkorDB, the app and Caddy (HTTPS through sslip.io).

  python deploy/ec2.py launch     # prints the URL when the instance is up
  python deploy/ec2.py status
  python deploy/ec2.py destroy

Needs AWS credentials with EC2 access. Uses the default VPC in AWS_REGION (default ap-south-1).
The app is cloned from the public repo, so push it first.
"""
import sys
import time

import boto3

REGION = "ap-south-1"
NAME = "interlock-demo"
REPO = "https://github.com/piyushk-dev/interlock"
TYPE = "t3.small"

USER_DATA = f"""#!/bin/bash
set -eux
dnf install -y docker git
systemctl enable --now docker
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-x86_64 -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
IP=$(curl -s http://checkip.amazonaws.com)
git clone {REPO} /opt/interlock
cd /opt/interlock
DOMAIN="$IP.sslip.io" docker compose --profile https up -d --build
"""

ec2 = boto3.client("ec2", region_name=REGION)


def instance():
    r = ec2.describe_instances(Filters=[{"Name": "tag:Name", "Values": [NAME]},
                                        {"Name": "instance-state-name", "Values": ["pending", "running", "stopped"]}])
    found = [i for x in r["Reservations"] for i in x["Instances"]]
    return found[0] if found else None


def launch():
    if instance():
        sys.exit("already running; use status")
    sg = ec2.create_security_group(GroupName=f"{NAME}-{int(time.time())}", Description="Interlock demo: HTTP and HTTPS")["GroupId"]
    ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": p, "ToPort": p, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]} for p in (80, 443)])
    ami = boto3.client("ssm", region_name=REGION).get_parameter(
        Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64")["Parameter"]["Value"]
    r = ec2.run_instances(ImageId=ami, InstanceType=TYPE, MinCount=1, MaxCount=1, SecurityGroupIds=[sg], UserData=USER_DATA,
                          BlockDeviceMappings=[{"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 16, "VolumeType": "gp3"}}],
                          TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": NAME}]}])
    iid = r["Instances"][0]["InstanceId"]
    ec2.get_waiter("instance_running").wait(InstanceIds=[iid])
    ip = instance()["PublicIpAddress"]
    print(f"{iid} running. In about 3 minutes: https://{ip}.sslip.io")


def status():
    i = instance()
    print("none" if not i else f"{i['InstanceId']} {i['State']['Name']} https://{i.get('PublicIpAddress')}.sslip.io")


def destroy():
    i = instance()
    if not i:
        return print("none")
    ec2.terminate_instances(InstanceIds=[i["InstanceId"]])
    print("terminating", i["InstanceId"])


if __name__ == "__main__":
    {"launch": launch, "status": status, "destroy": destroy}[sys.argv[1]]()
