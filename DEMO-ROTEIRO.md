# Roteiro — AWS DevOps Agent para um time DevOps/SRE sênior

**Duração:** 60 min (45 demo + 15 discussão) · **Público:** DevOps/SRE expert, EKS + ArgoCD · **Formato:** tudo ao vivo, slides só de apoio

**Mensagem central:** o DevOps Agent não é um chatbot em cima do CloudWatch. É um colega de on-call que vocês **programam** (skills, memórias, regras, agentes agendados), que **fecha o loop** (do alarme ao PR) e que **se integra ao que vocês já têm** (ArgoCD, GitHub, Slack, Kiro, MCP/A2A).

---

## Ambiente (IDs que você vai precisar)

| Item | Valor |
|---|---|
| Conta / região | `$(aws sts get-caller-identity --query Account --output text)` / `us-east-1` |
| Cluster EKS | `eks-agentic-troubleshooting` (3 nós, 1.34) |
| Namespace da app | `checkout` (ArgoCD Application `checkout-api`, auto-sync + self-heal + prune) |
| Repo GitOps | https://github.com/hellenavilarosa/checkout-devops-demo |
| Agent Space | `checkout-devops-demo` — id em `.agent-space-id` |
| Alarme | `checkout-api-error-rate` (>20% 5xx por 2 min) |
| Gatilho | EventBridge → Lambda `checkout-demo-alarm-to-investigation` → `CreateBacklogTask` |
| Investigação de referência (ensaio 13/10) | task `dc8cda5b-…`, execução `exe-ops1-5cb2a52a-…` |

Terminais abertos antes de começar (4 abas):

```bash
# T1 — cluster
export CTX=arn:aws:eks:us-east-1:$(aws sts get-caller-identity --query Account --output text):cluster/eks-agentic-troubleshooting
watch -n2 "kubectl --context \$CTX get pods -n checkout; echo; kubectl --context \$CTX get application checkout-api -n argocd -o custom-columns=SYNC:.status.sync.status,HEALTH:.status.health.status,REV:.status.sync.revision"

# T2 — o que o usuário vê
kubectl --context $CTX logs -n checkout deploy/loadgen -f | awk '{print $NF}'

# T3 — repo + helpers
cd ~/checkout-devops-demo

# T4 — Kiro CLI (Ato 3)
cd ~/checkout-devops-demo && kiro-cli chat
```

Abas do navegador: web app do DevOps Agent (Agent Space `checkout-devops-demo`), GitHub repo, ArgoCD UI (`kubectl port-forward -n argocd svc/argocd-server 8080:443`), CloudWatch alarme.

---

## Pré-flight (30 min antes)

- [ ] `overlays/prod/kustomization.yaml` em estado saudável: `REDIS_HOST=redis`, `APP_VERSION=1.0.0`. Alarme em `OK`. T2 mostrando `status=200`.
- [ ] Web app do DevOps Agent logado; investigação do ensaio (`[ALARM] checkout-api-error-rate`, 13/10) aberta numa aba como **fallback**.
- [ ] GitHub registrado no Agent Space e repo `checkout-devops-demo` conectado (sem isso o agente não aponta o commit culpado — ele ainda acha a causa pelo ConfigMap).
- [ ] Kiro CLI com o power do DevOps Agent instalado e autenticado (`kiro-cli chat` → `/tools` deve listar as ferramentas do DevOps Agent).
- [ ] Skill `runbook-argocd-rollback` e custom agent `eks-daily-health` já criados (Ato 2 mostra, não cria do zero — criar ao vivo custa 5 min de digitação).
- [ ] `gh auth status` OK (é por aqui que o Kiro CLI abre o PR).
- [ ] Vídeo de 90 s da investigação do ensaio salvo localmente, caso o Wi-Fi falhe.

---

## Abertura (3 min)

Sem slide de "o que é IA". Uma pergunta e um número:

> "Qual foi o último incidente de vocês em que achar a causa levou mais que consertar?"

Deixar responder. Depois: "Hoje vou quebrar um serviço em produção via Git, sem `kubectl`, e vocês vão ver o agente achar a causa, achar o commit, propor o fix e abrir o PR. No meio do caminho eu mostro como se programa esse agente — porque é isso que separa ferramenta de colega."

Mostrar em 30 s o que está rodando: T1 (pods verdes), T2 (200s), ArgoCD (Synced/Healthy), o repo. "Tudo isso entra por Git. Igual a vocês."

---

## Ato 1 — O incidente (18 min)

### 1.1 Quebrar via Git (2 min)

Em T3, mostrar o diff antes de commitar:

```bash
sed -i '' 's/REDIS_HOST=redis$/REDIS_HOST=redis-cache/; s/APP_VERSION=1.0.0/APP_VERSION=1.1.0/' overlays/prod/kustomization.yaml
git -P diff --no-index /dev/null overlays/prod/kustomization.yaml | grep -E '^\+.*(REDIS|VERSION)'
python3 scripts/gh-commit.py hellenavilarosa/checkout-devops-demo main \
  "chore(prod): point checkout-api at the new redis-cache service (v1.1.0)" overlays/prod/kustomization.yaml
kubectl --context $CTX annotate application checkout-api -n argocd argocd.argoproj.io/refresh=hard --overwrite
```

Narrativa: "Mensagem de commit plausível. Alguém achou que o Redis tinha sido renomeado. Passou no review."

Em ~30 s: T1 mostra rollout, T2 vira `503`. "Repare: pod `1/1 Running`. Pra o Kubernetes está tudo bem."

### 1.2 Detecção e gatilho automático (3 min — tempo de espera útil)

Enquanto o alarme não dispara (2 min de janela), mostrar a arquitetura do gatilho **no console**, não em slide:

- CloudWatch: metric filters `checkout-api-5xx` / `checkout-api-requests` em cima dos logs do Container Insights → metric math de taxa de erro.
- EventBridge rule `checkout-demo-alarm-to-investigation` → Lambda de 50 linhas → `CreateBacklogTask(INVESTIGATION)`.

Ponto pra time expert: "Isso é o padrão genérico. Se vocês usam PagerDuty, Datadog, Grafana ou Dynatrace, a integração é nativa e não precisa nem da Lambda. ServiceNow também."

Quando o alarme virar ALARM: abrir o web app, mostrar a investigação aparecendo sozinha em **Backlog** com o título `[ALARM] checkout-api-error-rate`.

### 1.3 A investigação ao vivo (8 min)

Abrir a investigação. Ela leva ~9 min do início ao plano; a timeline vai preenchendo enquanto você narra. Em paralelo, no T3:

```bash
python3 scripts/investigation-tail.py --n 8   # repetir a cada ~1 min
```

O que apontar, conforme aparece (ordem do ensaio):

1. **Hipóteses iniciais** — o agente lê o alarme, puxa as métricas, nota que o erro começou com um deploy. "Ele faz o que o on-call faz nos primeiros 5 minutos."
2. **Subagentes em paralelo** — `logs`, `argocd-change`, `propose-mitigation`. Mostrar que são investigações independentes convergindo.
3. **Root cause** — ConfigMap `REDIS_HOST: redis-cache`; confirma que o Service `redis-cache` não existe; logs mostram `Name does not resolve` em 100% das requests.
4. **O achado inesperado** (não pular isso): no ensaio o agente percebeu que uma **segunda mudança** — a readiness probe trocada de `/health` pra `/ready` — foi o que permitiu o rollout ruim chegar aos usuários. "Ninguém pediu isso. Ele correlacionou dois commits."
5. **O que ele NÃO conseguiu** — não leu o `syncPolicy` da Application por RBAC e disse isso explicitamente. "Ele declara os limites em vez de inventar. Pra confiar num agente em produção, isso importa mais que acertar."
6. **Plano de mitigação** — artefato separado: reverter `REDIS_HOST`, por que não basta `kubectl rollout undo` (ArgoCD self-heal desfaz), e o follow-up da probe.

### 1.4 Fork e Review (3 min) — lançado em outubro

- **Fork**: clicar numa mensagem da timeline no meio da investigação (ex.: onde ele decide investigar o ConfigMap) → *Fork investigation* → "E se fosse problema de rede do CoreDNS?". Mostrar que a fork roda em paralelo com o mesmo contexto até aquele ponto. "Vocês discordam de uma hipótese sem jogar fora o trabalho feito."
- **Review**: na investigação concluída do ensaio (fallback), *Launch Review* → o agente avalia a qualidade da própria investigação. "Isso alimenta o dashboard de qualidade que vocês vão ver no Ato 2."

### 1.5 Chat (2 min)

Na própria investigação, perguntar em linguagem natural:

> "Quantas requests de checkout falharam desde o início do incidente? Qual o impacto se isso durasse 1h?"

Mostrar a resposta com números tirados das métricas. Mencionar que o mesmo chat funciona no Slack (integração nativa, thread por investigação).

---

## Ato 2 — Programando o agente (14 min)

Mensagem: "Agora a parte que muda a relação de vocês com a ferramenta."

### 2.1 Skills em Git (4 min)

Abrir **Knowledge → Skills** no Agent Space. Mostrar a skill `runbook-argocd-rollback` (criada no pré-flight):

- É Markdown: quando aplicar, passos, comandos, o que NÃO fazer (`kubectl rollout undo` com self-heal ligado).
- Está versionada em Git (mostrar o repo de skills conectado). "O runbook de vocês vira comportamento do agente. Mudou o runbook, mudou o agente. Com PR, review e blame."
- Mostrar a skill `Agent Space Understanding` que o próprio agente gerou ao entrar no ambiente (task `SYSTEM_LEARNING` do backlog) — "ele estuda o ambiente antes do primeiro incidente".

### 2.2 Memórias (2 min)

**Knowledge → Memories**. Mostrar memórias que o agente capturou do incidente (ex.: "checkout-api depende do Service `redis` no ns `checkout`"). Adicionar uma ao vivo:

> "Deploys do checkout-api acontecem via ArgoCD com self-heal; mudanças manuais no cluster são revertidas automaticamente."

"Da próxima vez ele não precisa redescobrir isso."

### 2.3 Regras de incident-skip (2 min)

**Settings → Incident rules**. Criar ao vivo:

> Ignorar alarmes `checkout-api-error-rate` que disparem até 10 min após um sync do ArgoCD com rótulo `canary=true`.

"Vocês controlam o ruído. Deploy em janela conhecida não vira investigação."

### 2.4 Custom SRE agent agendado (3 min)

**Agents → Custom agents**. Mostrar `eks-daily-health` (pré-criado):

- Schedule diário 08:00 BRT.
- Instrução: revisar últimas 24h de Container Insights do cluster, pods com restart > 3, PVCs > 80%, nodes com pressure, alarmes que oscilaram; entregar relatório no Slack `#devops-eks`.
- Clicar **Run now** e deixar rodando em background — mostrar o resultado na discussão final.

Exemplos pro time: "relatório semanal de queries lentas do RDS", "auditoria de HPAs que nunca escalaram". `list-goals` já mostra um goal `ONCALL_REPORT` ativo (relatório semanal de pós-incidente) — mencionar.

### 2.5 Dashboards e labeling (2 min)

**Quality**: dashboard de tarefas, labeling humano (👍/👎 com motivo) por investigação. Rotular a investigação do ensaio ao vivo. "Vocês medem a acurácia do agente com os dados de vocês, não com o marketing da AWS."

### 2.6 Release management — preview (1 min, só mostrar)

**Release**: o agente avalia PRs antes de prod (risco da mudança, dependências, testes). Dizer que vai aparecer no Ato 3 em cima do PR do fix. Se estiver indisponível na região, dizer que é preview e mostrar o screenshot.

---

## Ato 3 — Fechando o loop (10 min)

### 3.1 Kiro CLI + power do DevOps Agent (5 min)

Em T4, dentro do `kiro-cli chat`:

> "Use o DevOps Agent para listar as investigações abertas no Agent Space checkout-devops-demo e me traga o plano de mitigação da mais recente."

Mostrar o plano chegando no terminal via MCP. Depois:

> "Aplique a mitigação neste repo: corrija `overlays/prod/kustomization.yaml`, crie um branch `fix/redis-host`, commite usando `scripts/gh-commit.py` e abra um PR com `gh pr create` referenciando a investigação."

Narrativa: "O mesmo agente que investigou está sendo consumido de dentro da ferramenta do dev. Headless, via MCP. Dá pra fazer isso de um pipeline, de um bot do Slack, do Claude, de qualquer coisa."

Se o Kiro CLI travar: fazer manualmente em T3 (`sed` + `gh-commit.py` no branch + `gh pr create`). O ponto é o PR existir.

### 3.2 Release management no PR (2 min)

Abrir o PR no GitHub. Se o release management estiver ativo, mostrar o check/comentário do DevOps Agent avaliando a mudança. Senão, mostrar o PR com o link da investigação na descrição e seguir.

### 3.3 Merge → ArgoCD → verde (3 min)

```bash
gh pr merge <n> --squash --delete-branch
kubectl --context $CTX annotate application checkout-api -n argocd argocd.argoproj.io/refresh=hard --overwrite
```

T1: rollout. T2: volta a `200`. Alarme volta a OK em ~2 min (mostrar no final da discussão). Investigação pode ser marcada como resolvida — mostrar o status mudando.

"Único toque humano: o merge. Todo o resto foi detecção, diagnóstico, correção e deploy automatizados — e auditável no Git."

---

## Fechamento (3 min) + discussão (15 min)

Slide único com o diagrama do que rodou:

```
git push ──► ArgoCD ──► EKS ──► Container Insights ──► Alarme
                                                         │
   PR ◄── Kiro CLI (MCP) ◄── Plano ◄── Investigação ◄── EventBridge/Lambda
    │
    └──► merge ──► ArgoCD ──► verde
```

Três frases de fechamento:
1. "Investigou em 9 min, achou a causa e um fator contribuinte que ninguém pediu."
2. "Vocês programam o comportamento com runbooks em Git, memórias e regras — não com prompt."
3. "Pode estar incluso no plano de Support de vocês." *(confirmar o tier do cliente antes da reunião)*

Pontos que provavelmente vão perguntar — ter resposta pronta:

| Pergunta | Resposta curta |
|---|---|
| Que permissões o agente tem no cluster/conta? | Role assumível por Agent Space (`DevOpsAgentRole-AgentSpace`, policy gerenciada `AIDevOpsAgentAccessPolicy` + inline). Read-only por padrão; mitigação é plano, não execução. Mostrar o trust com `aws:SourceArn`. |
| Ele executa mudanças sozinho? | Não por padrão. O loop de fix passa por PR. Automação adicional é decisão de vocês (ex.: auto-merge em L1). |
| GitLab self-managed / rede privada? | Suportado via Private Connection (VPC Lattice). GitHub Enterprise Server idem. |
| Multicloud / on-prem? | Suportado via integrações de observabilidade (Datadog, Dynatrace, Grafana, Splunk, New Relic) e MCP servers próprios. |
| Dados saem da conta? | Agent Space é isolado por conta/tenant; dados de telemetria são lidos via role, não copiados. Apontar a doc de tenancy do A2A. |
| Custo? | Pode estar incluso em planos Business/Enterprise Support (confirmar); fora disso, por uso. Encaminhar pra calculadora. |
| A2A — como plugo nossos agentes? | `register-service remoteagent` + A2A server do DevOps Agent (doc de 29/09). Sub-agentes em Bedrock ou qualquer framework. |

---

## Plano B por etapa

| Se falhar… | Faça |
|---|---|
| Alarme não dispara em 3 min | `aws cloudwatch set-alarm-state --alarm-name checkout-api-error-rate --state-value ALARM --state-reason demo` (dispara o EventBridge de verdade) |
| Investigação não abre sozinha | Web app → Backlog → New investigation, colar o título. Explicar que o gatilho é opcional. |
| Investigação muito lenta | Trocar pra aba com a investigação do ensaio (13/10), já concluída. Narrar a mesma sequência. |
| Kiro CLI sem MCP | Mostrar `scripts/investigation-tail.py` (mesma API, em Python) e fazer o PR manualmente. |
| Sem internet | Vídeo de 90 s + repo local + `kubectl` no cluster via VPN (se houver). |

## Reset pós-demo

```bash
# estado saudável no Git (se o PR não foi mergeado)
sed -i '' 's/REDIS_HOST=redis-cache/REDIS_HOST=redis/; s/APP_VERSION=1.1.0/APP_VERSION=1.0.0/' overlays/prod/kustomization.yaml
python3 scripts/gh-commit.py hellenavilarosa/checkout-devops-demo main "chore(prod): restore REDIS_HOST=redis" overlays/prod/kustomization.yaml
```
