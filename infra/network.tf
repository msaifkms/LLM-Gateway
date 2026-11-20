# Two AZs, each with a public subnet (load balancer and gateway tasks) and a
# private subnet (database and Redis).
#
# There is no NAT Gateway: it costs about $33/month per AZ before data. The
# gateway tasks instead run in the public subnets with a public IP, so they can
# reach ECR, CloudWatch Logs, SSM, Secrets Manager and the LLM providers
# directly. Security groups keep them closed: inbound only from the load
# balancer on port 8000. VPC interface endpoints could replace the public IP for
# the AWS services, but would cost more than a NAT (about $7/month each per AZ)
# and still could not reach OpenAI or Anthropic. See docs/deploy.md.

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = local.name }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = { Name = local.name }
}

resource "aws_subnet" "public" {
  count = length(local.azs)

  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index)
  # Tasks get a public IP from ECS (assign_public_ip); nothing else should.
  map_public_ip_on_launch = false

  tags = { Name = "${local.name}-public-${local.azs[count.index]}", tier = "public" }
}

resource "aws_subnet" "private" {
  count = length(local.azs)

  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 10)

  tags = { Name = "${local.name}-private-${local.azs[count.index]}", tier = "private" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count = length(aws_subnet.public)

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Private subnets have only the implicit local route: no way in or out of the VPC.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  tags = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "private" {
  count = length(aws_subnet.private)

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# Strip every rule from the VPC's default security group, so anything that ends
# up in it by accident can neither send nor receive.
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.main.id

  tags = { Name = "${local.name}-default-deny-all" }
}
