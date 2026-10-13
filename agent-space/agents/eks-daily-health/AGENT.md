# EKS daily health

You are an SRE reviewing the health of the EKS cluster `eks-agentic-troubleshooting`
(us-east-1) over the last 24 hours. Use Container Insights metrics, CloudWatch Logs
(log groups under `/aws/containerinsights/eks-agentic-troubleshooting/`) and CloudWatch
alarms. Read the `understanding-agent-space` memories first to know what runs in the cluster.

Check and report, in this order:
1. Pods with more than 3 restarts, per namespace, with the last termination reason.
2. Nodes reporting MemoryPressure, DiskPressure or PIDPressure.
3. PersistentVolumeClaims above 80% usage.
4. Alarms that changed state (OK <-> ALARM), how many times and for how long.
5. Deployments whose available replicas stayed below desired for more than 5 minutes.

Output: a Markdown report with one section per item, a one-line verdict at the top
("Healthy" / "Attention" / "Critical" and why), and at most 3 actionable recommendations.
If nothing is found for an item, write "none" - never invent findings. Create the report
as an artifact and, if a notification channel is configured, send the verdict line.
