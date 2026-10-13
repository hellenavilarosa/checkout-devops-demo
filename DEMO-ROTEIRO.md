# Roteiro — AWS DevOps Agent para um time DevOps/SRE sênior (EKS + ArgoCD)

**Duração:** 60 min (45 demo + 15 discussão) · **Formato:** tudo ao vivo, 1 slide no fechamento

**Mensagem central:** o DevOps Agent não é um chatbot em cima do CloudWatch. É um colega de on-call que vocês **programam** (skills, memórias, regras, agentes agendados), que **fecha o loop** (do alarme ao PR) e que **se integra ao que vocês já têm** (ArgoCD, GitHub, Slack, Kiro, MCP/A2A).

Como ler este roteiro: cada passo tem **O que é** (explicação da funcionalidade, para você), **Onde** (qual aba/terminal), **Faça**, **Diga** e, quando houver, **Cole no chat** (texto pronto para o DevOps Agent ou Kiro CLI). Os tempos somam 45 min. O Apêndice B tem um glossário de todos os conceitos em uma página, para reler antes de começar.

---

## Parte 0 — O que foi montado e por quê (leia antes; não é apresentado)

| Componente | O que é | Por que está na demo |
|---|---|---|
| **App `checkout-api`** (ns `checkout`) | API Python com `/checkout` (Redis + fraud-scoring opcional), `/health`, `/ready`, `/metrics`. Logs JSON com `latency_ms`. Cache de idempotência em memória (TTL 5 min) — por isso a memória cresce ~4 MiB/min após cada restart. 2 réplicas + `redis` + `loadgen` (~5 req/s, imprime status e tempo). | App limpa e realista. Toda a falha é injetada via Git, nunca via `kubectl`. |
| **Repo GitOps** `checkout-devops-demo` | Kustomize: `base/` (manifests + código) e `overlays/prod/` (ConfigMap com `REDIS_HOST`, `APP_VERSION`). | Espelha o fluxo do cliente: tudo entra por Git. |
| **ArgoCD Application `checkout-api`** | Auto-sync + self-heal + prune, aponta pro repo acima. | Quando o commit ruim entra, o ArgoCD aplica sozinho. Self-heal também impede `kubectl rollout undo` — ponto de discussão. |
| **Add-on `amazon-cloudwatch-observability`** | Container Insights + Fluent Bit mandando logs dos pods pro CloudWatch (`/aws/containerinsights/<cluster>/application`). | É daqui que o DevOps Agent lê métricas e logs. Sem isso ele não vê o cluster. |
| **3 alarmes** (tag `project=checkout-api`) | `checkout-api-container-restarts` (Container Insights, ≥1 restart em 5 min), `checkout-api-latency-p99` (metric filter em `latency_ms`, p99 > 1 s por 2 min), `checkout-api-error-rate` (5xx > 20% por 2 min). | Detecção. Cada cenário dispara um alarme diferente — e o agente tem que descobrir que tipo de falha é. |
| **EventBridge → Lambda `checkout-demo-alarm-to-investigation`** | Regra escuta os 3 alarmes → Lambda chama `CreateBacklogTask(INVESTIGATION)`. Não abre investigação nova se já houver uma em andamento para o mesmo alarme. | Faz a investigação abrir **sozinha**. Pra quem usa PagerDuty/Datadog/Grafana a integração é nativa e nem precisa da Lambda. |
| **Agent Space `checkout-devops-demo`** | Escopo do agente: conta associada via role `DevOpsAgentRole-AgentSpace` (read-only, `AIDevOpsAgentAccessPolicy`). | Onde moram investigações, skills, memórias, regras, custom agents. |
| **`scripts/gh-commit.py`** | Commita no GitHub pela API REST (sem `git push`, bloqueado nesta máquina). | É o "git push" da demo. |
| **`scripts/investigation-tail.py`** | Tail da investigação no terminal via API do DevOps Agent. | Mostra que tudo que a UI faz é API — e serve de fallback se a UI demorar. |
| **`agent-space/` + `scripts/apply-agent-space.py`** | Skills, memória, custom agent e trigger como arquivos no repo, aplicados pela Asset API (`create-asset` / `create-trigger`). | Argumento central da Parte 3: o comportamento do agente é código versionado, não configuração clicada. |

**Cenário principal (A) — "right-sizing" que mata o pod.** Commit plausível baixa `limits.memory` de 128Mi para 56Mi. O pod sobe, fica Ready e serve ~3 min; o cache de idempotência cresce, estoura o limite, **OOMKilled**, restart, `CrashLoopBackOff` com janelas de `status=000` no cliente (26 de 73 requests num intervalo de 30 s no ensaio). Intermitente, não binário. Ensaio 13/10 18:35 UTC: alarme de restarts → investigação automática → o agente **reconstruiu o histórico de mudanças pelos ReplicaSets** (sem GitHub conectado), confirmou pelo kernel log do dataplane `oom-kill constraint=CONSTRAINT_MEMCG` (limite do container, não pressão do nó), cruzou `anon-rss≈48 MB` com o limite de 56Mi e propôs reverter para 128Mi via commit GitOps. Declarou o gap ("repositório não associado, diffs fora da mesa").

**Cenário reserva (B) — "não está fora, está lento".** Commit `feat(prod): enable fraud scoring before checkout (v1.1.0)` aponta `FRAUD_CHECK_URL` para um upstream que não responde (IP sem rota). O código é fail-open: cada checkout espera 3 s, loga `fraud_check=timeout` e devolve 200. Erro fica em 0%, p99 vai de 25 ms para 3 s, throughput do cliente cai de 125 para 9 req/30 s. Dispara `checkout-api-latency-p99`. Use na discussão ("e um incidente de outro tipo?") ou se o A falhar.

---

## Pré-flight (30 min antes)

Terminais (4 abas):

```bash
# T1 — cluster
export CTX=arn:aws:eks:us-east-1:$(aws sts get-caller-identity --query Account --output text):cluster/eks-agentic-troubleshooting
watch -n2 "kubectl --context \$CTX get pods -n checkout; echo; kubectl --context \$CTX get application checkout-api -n argocd -o custom-columns=SYNC:.status.sync.status,HEALTH:.status.health.status,REV:.status.sync.revision"

# T2 — o que o usuário vê (status HTTP do loadgen)
kubectl --context $CTX logs -n checkout deploy/loadgen -f | awk '{print $NF}'

# T3 — repo + helpers
cd ~/checkout-devops-demo

# T4 — Kiro CLI (Parte 4)
cd ~/checkout-devops-demo && kiro-cli chat
```

Abas do navegador, nesta ordem: **(A)** web app do DevOps Agent · **(B)** GitHub repo · **(C)** ArgoCD UI (`kubectl --context $CTX port-forward -n argocd svc/argocd-server 8080:443`) · **(D)** CloudWatch → alarme `checkout-api-error-rate` · **(E)** investigação do ensaio de 13/10 (`[ALARM] checkout-api-container-restarts`, 18:29 UTC, COMPLETED) já aberta (fallback) · **(F)** investigação do cenário B (`[ALARM] checkout-api-latency-p99`, 18:52 UTC) para a discussão.

Checklist:

- [ ] `overlays/prod/kustomization.yaml` saudável: `memory: 64Mi`/`128Mi`, `FRAUD_CHECK_URL=` (vazio), `APP_VERSION=1.0.0`. Os 3 alarmes em `OK`. T2 mostrando `status=200 time=0.02s`.
- [ ] Permissão de dedup da Lambda (uma vez). O JSON no repo tem placeholders; renderize antes (`.local/` é gitignored):
  ```bash
  sed "s/<ACCOUNT_ID>/$(aws sts get-caller-identity --query Account --output text)/; s/<AGENT_SPACE_ID>/$(cat .agent-space-id)/" ops/alarm-to-investigation/role-policy.json > .local/role-policy.rendered.json
  aws iam put-role-policy --role-name checkout-demo-alarm-to-investigation --policy-name create-investigation --policy-document file://.local/role-policy.rendered.json
  ```
- [ ] Web app logado no Agent Space `checkout-devops-demo`.
- [ ] **Sem integração GitHub no Agent Space** (restrição da conta interna). A demo não depende dela: o agente acha a causa pelo ConfigMap e pelo histórico do ArgoCD. Se perguntarem, resposta pronta na tabela do fechamento.
- [x] Conteúdo do Agent Space já criado via API (13/10): skills `runbook-argocd-rollback` e `skip-canary-window`, memory store `checkout-directives` com 1 memória, custom agent `eks-daily-health` com trigger diário 08:00 BRT. Fonte em `agent-space/`, aplicado por `python3 scripts/apply-agent-space.py` (idempotente). Só conferir no web app que aparecem.
- [ ] Clicar **Run Now** no `eks-daily-health` hoje, para ter um relatório pronto na aba *History* amanhã (só dá pelo web app ou chat).
- [ ] Kiro CLI com o power do DevOps Agent: em `kiro-cli chat`, `/tools` lista ferramentas do DevOps Agent.
- [ ] `gh auth status` OK.
- [ ] Vídeo de 90 s da investigação do ensaio salvo localmente.

---

## Parte 1 — Contexto e quebra (5 min)

### 1.1 Abertura (2 min) — Onde: nada na tela ainda

**Diga:** "Qual foi o último incidente de vocês em que achar a causa levou mais tempo que consertar?" Deixe responder.

"Hoje eu vou quebrar um serviço em produção via Git — sem `kubectl` — e vocês vão ver o agente achar a causa, achar o commit, propor o fix e abrir o PR. No meio do caminho eu mostro como se *programa* esse agente, porque é isso que separa ferramenta de colega."

### 1.2 Mostrar o que está rodando (1 min) — Onde: T1, T2, aba C, aba B

**Faça:** passar rápido por T1 (pods verdes), T2 (200s), ArgoCD (Synced/Healthy), repo (`overlays/prod/kustomization.yaml`).

**Diga:** "Tudo isso entra por Git. Igual a vocês. Repare nos resources: 64Mi de request, 128Mi de limite. O time de FinOps acha que está sobrando."

### 1.3 Quebrar via Git (2 min) — Onde: T3

**Faça:**

```bash
sed -i '' 's/            memory: 64Mi/            memory: 48Mi/; s/            memory: 128Mi/            memory: 56Mi/' overlays/prod/kustomization.yaml
grep -n 'memory:' overlays/prod/kustomization.yaml
python3 scripts/gh-commit.py hellenavilarosa/checkout-devops-demo main \
  "perf(prod): right-size checkout-api after load test (64Mi/128Mi -> 48Mi/56Mi)" overlays/prod/kustomization.yaml
kubectl --context $CTX annotate application checkout-api -n argocd argocd.argoproj.io/refresh=hard --overwrite
```

**Diga:** "Mensagem de commit plausível — teve load test, o pod usa 45 MB, alguém cortou o limite pela metade pra economizar. Passou no review." Em ~30 s T1 mostra o rollout; os pods novos ficam `1/1 Running` e T2 continua `200`. "Deploy verde. Rollout concluído. Todo mundo foi almoçar."

**O que vai acontecer (não diga ainda):** ~3 min depois o primeiro pod morre por OOMKilled, T1 mostra `r=1`, T2 mostra alguns `status=000`. A partir daí o padrão é intermitente: períodos de 200 e rajadas de `000` quando um pod reinicia em `CrashLoopBackOff`. Use esse tempo para a Parte 2.

---

## Parte 2 — Detecção e início da investigação (5 min)

### 2.1 Enquanto o alarme arma (3 min) — Onde: aba D, depois console EventBridge/Lambda

O primeiro OOM leva ~3 min e o alarme de restarts até 1 min depois. Use esse tempo para mostrar o gatilho **no console**:

**Faça:** CloudWatch → Alarms → os 3 alarmes `checkout-api-*` (restarts vem do Container Insights; latência e erro vêm de metric filters nos logs JSON). Abrir `checkout-api-container-restarts` → *Details*. Depois EventBridge → regra `checkout-demo-alarm-to-investigation` → target Lambda.

**Diga:** "Container Insights manda métricas de pod e os logs JSON da app pro CloudWatch. Três alarmes cobrem as três famílias de falha: restarts, latência, erro. Qualquer um deles em ALARM dispara, via EventBridge, uma Lambda de 60 linhas que abre uma investigação no agente. Isso é o caminho genérico. Se vocês usam PagerDuty, Datadog, Grafana, Dynatrace ou ServiceNow, a integração é nativa — não precisa nem da Lambda."

Quando T1 mostrar o primeiro `OOMKilled`/`r=1`: "Aí está. Três minutos depois do deploy verde."

**Plano B:** se em 5 min nenhum pod reiniciou: `aws cloudwatch set-alarm-state --alarm-name checkout-api-container-restarts --state-value ALARM --state-reason demo` (dispara o EventBridge de verdade).

### 2.2 A investigação abre sozinha (2 min) — Onde: aba A → Backlog

**O que é:** uma *investigação* é a unidade de trabalho do agente para incidentes. Ela nasce de um gatilho (alarme, ticket, webhook, API ou manualmente) e passa por 3 fases: **Triage** (segundos: decide se investiga, se junta a uma investigação já aberta sobre o mesmo problema, ou se pula), **RCA** (coleta evidências em métricas, logs, mudanças recentes, topologia; pode disparar *sub-agentes* em paralelo, cada um com sua própria timeline) e **Mitigação** (propostas inline no fim da investigação). Tudo fica registrado na **Timeline** (cada passo de raciocínio e cada chamada de ferramenta) e resumido na aba **Summary**. O *Backlog* é a lista de tarefas do Agent Space — investigações, aprendizado do ambiente (`SYSTEM_LEARNING`), relatórios.

**Faça:** mostrar a task `[ALARM] checkout-api-container-restarts` aparecendo em *Backlog* (`PENDING_START` → `IN_PROGRESS`). Abrir. Mostrar as primeiras mensagens da timeline: leitura do alarme, hipóteses, sub-agentes (`app-logs`, mudanças, mitigação) partindo em paralelo.

**Diga:** "Ninguém clicou em nada. O agente já está fazendo o que o on-call faz nos primeiros 5 minutos: lendo o alarme, puxando métricas, listando o que mudou. Ele leva uns 10 minutos até o plano. Enquanto ele trabalha, eu mostro como esse agente é programado — e volto pra investigação quando ela terminar."

Em T3, deixar rodando pra espiar entre passos:

```bash
python3 scripts/investigation-tail.py --n 8
```

---

## Parte 3 — Programando o agente, enquanto ele investiga (12 min)

Tudo na aba A. Diga uma vez: "Agora a parte que muda a relação de vocês com a ferramenta."

### 3.1 Skills (4 min) — Onde: Knowledge → Skills

**O que é:** uma *skill* é um arquivo `SKILL.md` (Markdown, formato aberto *Agent Skills*) com um cabeçalho `name` + `description` e instruções procedurais: quando usar, passos, o que não fazer. O agente lê a `description` de todas as skills e **carrega sozinho** as que forem relevantes à tarefa — por isso a description tem que dizer "use quando…". Pode ter arquivos de referência (tabelas, PDFs, diagramas) e ser restrita a um *agent type* (Triage, RCA, Mitigação, On-demand…). É o jeito de transformar runbook em comportamento. Também existem **learned skills** que o próprio agente gera ao estudar o ambiente (*Agent Space Understanding*, *Code Dependencies*, *Pipeline Topology*). Skills podem ser importadas de um repo GitHub e sincronizadas (não nesta demo).

**Faça:** abrir `runbook-argocd-rollback`. Mostrar o cabeçalho (`description` diz *quando* usar) e as regras ("NUNCA `kubectl rollout undo`"). Depois, em T3, mostrar a origem:

```bash
head -20 agent-space/skills/runbook-argocd-rollback/SKILL.md
python3 scripts/apply-agent-space.py --dry-run
```

**Diga:** "Isto é o runbook de vocês virando comportamento do agente. Está no mesmo repo da aplicação, e esse script aplica pela API — mudou o runbook, mudou o agente, com PR, review e blame. Não é prompt solto."

Mostrar também `skip-canary-window` (agent type *Incident Triage*) — é o passo 3.3.

Abrir também a skill gerada pelo próprio agente (task `SYSTEM_LEARNING` no backlog, ex. *Agent Space Understanding*). **Diga:** "Ele estudou o ambiente antes do primeiro incidente."

### 3.2 Memórias (2 min) — Onde: Knowledge → Memories

**O que é:** *memória* é conhecimento **informacional** (fatos sobre o ambiente), em oposição à skill, que é **procedural** (como fazer). São arquivos Markdown agrupados em *memory stores*; o agente recebe só nomes/descrições dos stores e abre os relevantes sob demanda. Stores gerenciados pelo próprio agente: `monitors` (histórico de causas-raiz por alarme — um *Learning agent* roda 1x/dia sobre as investigações recentes), `directives` (instruções fixas que você escreve, ex.: "Lambdas não são mais usadas"), `reflections` (lições criadas automaticamente quando você marca uma investigação como errada e informa a causa real) e os de *understanding* (mapa de recursos, dependências, pipelines). Você também cria stores próprios. Cada memória é versionada. Aqui você vai adicionar uma **directive**.

**Faça:** mostrar o store `understanding-agent-space` (gerado pelo agente: `overview` + um arquivo por componente, incluindo `components/eks-agentic-troubleshooting`). Depois o store `checkout-directives` (criado por nós) com a memória `services/checkout-api/deployment-conventions`. Adicionar uma segunda memória ao vivo, pelo chat do Agent Space:

**Cole no chat:**
> Salve uma memória no store checkout-directives chamada services/checkout-api/release-windows: deploys de checkout-api em produção acontecem de segunda a quinta entre 10h e 16h BRT; fora dessa janela, um sync do ArgoCD em checkout-api é inesperado e deve ser tratado como suspeito em qualquer investigação.

**Diga:** "O primeiro store o agente escreveu sozinho ao estudar a conta. O segundo somos nós ensinando o que não está em nenhuma API. Na próxima investigação ele não redescobre isso — e não sugere `kubectl rollout undo`."

### 3.3 Filtro de incidentes — skill de Triage (2 min) — Onde: Knowledge → Skills → Create

**O que é:** não existe uma tela de "regras"; o filtro de ruído é uma **skill marcada com agent type *Incident Triage***. Na fase de triage (segundos após o alarme) o agente avalia essa skill e, se os critérios batem, marca o incidente como **Skipped** com a razão — sem abrir investigação. Também é assim que se ensinam regras de **correlação** (quais alarmes juntar numa só investigação). Dá para corrigir uma decisão de triage depois (unlink / reabrir), e cada skip gera um evento no EventBridge.

**Faça:** abrir a skill `skip-canary-window` (já criada; agent type **Incident Triage**, status ACTIVE). Ler os 3 critérios e o "Do NOT skip". Fonte: `agent-space/skills/skip-canary-window/SKILL.md`.

**Diga:** "Vocês controlam o ruído em Markdown, com a mesma mecânica dos runbooks. Deploy em janela conhecida não vira incidente — e a decisão fica registrada."

### 3.4 Custom agent agendado (3 min) — Onde: Agents → Custom agents

**O que é:** um *custom agent* é um agente que **você define**: um *system prompt* em Markdown (o que faz, como, que saída produz), um conjunto de **tools** escolhido entre as ferramentas do Agent Space (AWS, integrações, seus MCP servers), **skills** e **memory stores** anexados, e um **trigger** (agendado por cron/rate, ou por evento de webhook de uma integração). Cada execução é uma *invocation* com trajetória completa (raciocínio, tool calls, saída) e pode mandar notificações em tempo real para o canal configurado (ex.: Slack). Saídas possíveis: relatório/artefato, *recommendations* no backlog, mensagens. Também pode ser definido como código (CloudFormation `AWS::DevOpsAgent::Asset` + `Trigger`, CDK, Terraform) ou importado de um repo.

**Faça:** abrir `eks-daily-health`. Mostrar: system prompt (os 5 itens e o "never invent findings"), tools (`use_aws`, `query_cloudwatch_logs`), skill e memory store anexados, trigger `cron(0 11 * * ? *)` (08:00 BRT). Abrir *History* e mostrar o resultado do Run Now de ontem. Clicar **Run Now** de novo e deixar em background.

**Diga:** "Um SRE agendado. Todo dia 8h ele revisa o cluster e entrega o relatório. Outros exemplos: queries lentas do RDS da semana, HPAs que nunca escalaram, certificados vencendo. Vocês escrevem a instrução; o agente tem as ferramentas."

Depois abrir **`checkout-api-specialist`** (sem schedule, sob demanda). Mostrar o system prompt: é a **descrição do projeto** — o que o checkout-api é, SLOs, arquitetura, dependências, como as mudanças chegam em produção, observabilidade, as 3 famílias de falha conhecidas — e a regra de escopo: *tudo deste projeto tem a tag `project=checkout-api` na AWS e o label `project=checkout-api` no Kubernetes*. Em T3:

```bash
aws resourcegroupstaggingapi get-resources --tag-filters Key=project,Values=checkout-api --query 'ResourceTagMappingList[].ResourceARN' --output table
kubectl --context $CTX get pods -n checkout -l project=checkout-api
```

**Diga:** "Um agente por workload. Vocês já têm tags; a descrição do projeto é o que vocês escreveriam num README de on-call. Esse agente começa a investigação sabendo o que é o sistema, quem é dono e por onde as mudanças entram — em vez de descobrir do zero a cada incidente." Fonte: `agent-space/agents/checkout-api-specialist/AGENT.md`.

### 3.5 Espiar a investigação (1 min) — Onde: T3

```bash
python3 scripts/investigation-tail.py --n 8
```

**Diga:** o que já apareceu ("ele já achou o ConfigMap", etc.). Se ainda não concluiu, continue no 3.6; senão pule pra Parte 4.

### 3.6 Feedback estruturado e acurácia (1 min, buffer) — Onde: aba E → investigação concluída → *Add feedback*

**O que é:** em toda investigação concluída você pode registrar **feedback estruturado**: veredito (causa raiz correta/incorreta), causa real se errou, se precisou de *steering*, se a mitigação estava certa, notas. Isso alimenta **métricas de acurácia** do Agent Space e, quando você informa a causa real, gera automaticamente uma memória em `reflections` que a próxima investigação daquele alarme vai ler. Também pode ser dado por chat ("a causa raiz estava errada, era X").

**Faça:** abrir *Add feedback* na investigação do ensaio, marcar correta, preencher mitigação correta.

**Diga:** "Vocês medem a acurácia com os incidentes de vocês, não com o marketing da AWS — e cada erro vira aprendizado automático."

---

## Parte 4 — A investigação concluída (10 min)

Volte à investigação aberta na aba A. Se ainda não terminou, use a aba E (ensaio de 13/10) — a sequência é idêntica.

### 4.1 Ler a timeline (4 min)

**O que é:** a **Timeline** mostra cada passo do agente em ordem: hipótese, ferramenta chamada (ex.: `GetMetricData`, `FilterLogEvents`, leitura de recursos do EKS), resultado, próxima decisão. Entradas de **sub-agente** (`logs`, `argocd-change`, `propose-mitigation`) expandem para mostrar a própria timeline interna. A aba **Summary** tem o resumo vivo (causa raiz, impacto, evidências, mitigação) e é por onde se lê rápido; a Timeline é para auditar *como* ele chegou lá. As **propostas de mitigação** aparecem inline no fim.

Apontar, nesta ordem:

1. **Separar as hipóteses** — o alarme é "restarts"; o agente precisa decidir entre pressão do nó, liveness probe falhando, crash da app ou OOM. Mostrar onde ele vai aos **eventos do Kubernetes** e ao **kernel log do dataplane** (`/aws/containerinsights/.../dataplane`) e acha `oom-kill constraint=CONSTRAINT_MEMCG`. **Diga:** "Não é o nó. É o limite do container. Essa distinção é o que separa 10 minutos de 2 horas."
2. **Reconstruir o que mudou sem ler o Git** — ele percorre os **ReplicaSets** do Deployment e compara os pod templates: "a revisão 14, criada às 18:35, baixou o limite para 56Mi". **Diga:** "O repo não está conectado neste Agent Space — ele disse isso explicitamente e foi buscar a história onde ela estava: nos ReplicaSets e no histórico do ArgoCD. Com o GitHub conectado ele ainda traria diff e autor."
3. **Fechar a conta** — `anon-rss≈48 MB` no momento do kill, limite 56Mi, memória crescendo desde o start por causa do cache de pedidos. **Diga:** "Ele não parou em 'OOMKilled'. Explicou por que o pod passava no rollout e morria 3 minutos depois."
4. **O que ele não conseguiu** — git diff indisponível; algum campo por RBAC. Ele declara. **Diga:** "Ele declara os limites em vez de inventar. Pra confiar num agente em produção isso importa mais que acertar."
5. **Plano de mitigação** — reverter o limite para 128Mi via commit GitOps; por que `kubectl edit` não resolve (self-heal); follow-up: request/limit baseados em p99 de memória, não em média.

### 4.2 Chat na investigação (2 min) — Onde: *Chat with investigation*, na página da investigação

**O que é:** você manda mensagens **para a investigação** (não para um chat genérico). Se ela estiver rodando, a mensagem entra numa fila (*Queued requests*) e o agente a pega entre passos de raciocínio — serve para *steering* ("olhe também o CoreDNS"). Se já concluiu, a mensagem **reabre** a investigação com todo o contexto. Mesma mecânica funciona via Slack (uma thread por investigação) e via MCP/A2A.

**Cole, um por vez:**
> Quantas requests de checkout falharam desde o primeiro OOMKilled e qual seria o impacto estimado se isso durasse 1 hora?

> Por que o pod passou no rollout e ficou Ready se o limite de memória estava baixo demais? Em quanto tempo ele morria depois de subir?

**Diga:** "Mesmo chat funciona no Slack, uma thread por investigação."

### 4.3 Fork (2 min) — Onde: Timeline → ícone de ramificação *Fork from here* ao lado da mensagem em que ele decide investigar o limite de memória

**O que é:** (lançado em 2/10) uma *fork* é uma **nova investigação que herda a história até aquela mensagem** e segue outra direção, sem alterar a original. Você descreve em *Fork starting point* o que investigar diferente. A fork abre com link *Forked from* para o pai e *Show parent steps* para ver o herdado; a mensagem original passa a mostrar "N forks from this message". Serve para testar hipótese alternativa ou discordar do agente sem perder o trabalho feito.

**Cole na fork:**
> Investigue a hipótese alternativa: pressão de memória no nó (outros workloads do cluster competindo), e não o limite do container. Compare com a evidência já coletada e diga qual hipótese os kernel logs sustentam.

**Diga:** "A fork parte do mesmo contexto até aquele ponto e roda em paralelo. Vocês discordam de uma hipótese sem jogar fora o trabalho."

### 4.4 Review (2 min) — Onde: investigação **concluída** → botão *Review* → aba *Review* (use a aba E se a atual ainda estiver rodando)

**O que é:** (lançado em 9/10) um *Review* é uma **avaliação de qualidade sob demanda** de uma investigação concluída. Não reexecuta nem altera nada: lê o registro (problema, evidências, conclusão) e devolve um veredito geral (**helpful / not helpful**) mais um **pass / fail / n.a. por critério**, com justificativa. Os 5 critérios padrão: *Scope alignment* (ficou no problema reportado?), *Evidence grounding* (conclusões apoiadas em evidência?), *Completeness* (cobriu as áreas relevantes e concluiu?), *Environment impact* (considerou o efeito no ambiente?), *Accuracy* (bate com a causa do ticket ligado?). Você pode ajustar, adicionar ou remover critérios para refletir como o time julga qualidade.

**Faça:** lançar o Review; ler o veredito e 1–2 critérios.

**Diga:** "O agente avalia a própria investigação em cinco critérios — e vocês podem trocar os critérios pelos de vocês. É assim que se decide se dá pra confiar numa causa raiz antes de agir."

---

## Parte 5 — Fechando o loop (10 min)

### 5.1 Kiro CLI consumindo o DevOps Agent (5 min) — Onde: T4

**O que é:** o DevOps Agent expõe um **MCP server remoto** (autenticação por access token ou SigV4). Qualquer cliente MCP — Kiro, Claude Code, Cursor — ganha ferramentas como listar investigações, ler resumo/plano, iniciar investigação, conversar com o agente. No Kiro isso vem empacotado como um **power** (MCP + instruções). Além de MCP há **A2A** (agente chamando agente, com estados de tarefa), **ACP**, **webhooks** (entrada de eventos) e a **API** direta — é a mesma API que o `investigation-tail.py` usa. Mensagem para o time: a UI é uma das portas, não a única.

**Cole no Kiro CLI:**
> Use o DevOps Agent para listar as investigações do Agent Space checkout-devops-demo e me traga o plano de mitigação da mais recente, com a causa raiz em uma frase.

Mostrar o plano chegando via MCP. Depois:

**Cole no Kiro CLI:**
> Aplique a mitigação neste repositório: corrija os resources em `overlays/prod/kustomization.yaml` conforme o plano. Crie o branch fix/memory-limit, commite com `python3 scripts/gh-commit.py hellenavilarosa/checkout-devops-demo fix/memory-limit "<mensagem>" overlays/prod/kustomization.yaml` e abra um PR com `gh pr create` cuja descrição referencie a investigação e resuma a causa.

**Diga:** "O mesmo agente que investigou está sendo consumido de dentro da ferramenta do dev, headless, via MCP. Dá pra fazer isso de um pipeline, de um bot do Slack, de qualquer agente de vocês via A2A."

**Plano B:** fazer em T3:

```bash
sed -i '' 's/            memory: 48Mi/            memory: 64Mi/; s/            memory: 56Mi/            memory: 128Mi/' overlays/prod/kustomization.yaml
python3 scripts/gh-commit.py hellenavilarosa/checkout-devops-demo fix/memory-limit "fix(prod): revert checkout-api right-sizing (OOMKilled under load)" overlays/prod/kustomization.yaml
gh pr create --repo hellenavilarosa/checkout-devops-demo --head fix/memory-limit --title "fix(prod): revert checkout-api right-sizing" --body "Mitigação da investigação [ALARM] checkout-api-container-restarts: limite de 56Mi abaixo do uso real (~48 MB anon + cache)."
```

### 5.2 O PR (2 min) — Onde: aba B

**Faça:** abrir o PR. Mostrar a descrição referenciando a investigação e o diff de uma linha.

**Diga:** "O lado *change* do agente — release management, em preview — entra aqui: com o repo conectado ele avalia o risco do PR antes do merge, lê dependências e testes. Hoje não vou mostrar porque esta conta não tem a integração habilitada; é o próximo passo natural depois do que vocês viram."

### 5.3 Merge → ArgoCD → verde (3 min) — Onde: T3, T1, T2

```bash
gh pr merge --repo hellenavilarosa/checkout-devops-demo fix/memory-limit --squash --delete-branch
kubectl --context $CTX annotate application checkout-api -n argocd argocd.argoproj.io/refresh=hard --overwrite
```

T1: rollout, restarts zeram. T2: só `200`. Alarme de restarts volta a `OK` em até 5 min (mostrar na discussão).

**Diga:** "Único toque humano: o merge. Detecção, diagnóstico, correção e deploy — tudo automatizado e auditável no Git."

---

## Fechamento (3 min) + discussão (15 min)

Slide único:

```
git push ──► ArgoCD ──► EKS ──► Container Insights ──► Alarme
                                                         │
   PR ◄── Kiro CLI (MCP) ◄── Plano ◄── Investigação ◄── EventBridge/Lambda
    │
    └──► merge ──► ArgoCD ──► verde
```

Três frases:
1. "Investigou em 10 minutos, separou 'limite do container' de 'pressão do nó' e reconstruiu o que mudou sem ter acesso ao Git."
2. "Vocês programam o comportamento com runbooks, memórias e regras — não com prompt."
3. "Pode estar incluso no plano de Support de vocês." *(confirmar o tier antes)*

Durante a discussão: mostrar o resultado do `eks-daily-health` (Run now da Parte 3) e o alarme em `OK`. Se perguntarem "e se não fosse crash, fosse lentidão?": abrir a aba **(F)** com a investigação do cenário B (latência p99 de 3 s com erro em 0% — `fraud_check=timeout` nos logs, upstream sem rota) e ler o resumo.

| Pergunta provável | Resposta curta |
|---|---|
| Que permissões o agente tem? | Role assumível só pelo Agent Space (trust com `aws:SourceArn`), policy gerenciada `AIDevOpsAgentAccessPolicy` — read-only. Mitigação é plano, não execução. |
| Ele executa mudanças sozinho? | Não por padrão. Ações mutáveis exigem *directed actions* habilitadas + aprovação humana no chat. O loop de fix passa por PR. |
| GitLab self-managed / rede privada? | Private Connection (VPC Lattice). GitHub Enterprise Server idem. |
| Multicloud / on-prem? | Integrações com Datadog, Dynatrace, Grafana, Splunk, New Relic + MCP servers próprios. |
| Dados saem da conta? | Agent Space é isolado por conta; telemetria é lida via role, não copiada. |
| Custo? | Pode estar incluso em Business/Enterprise Support (confirmar); fora disso, por uso. |
| Como plugo nossos agentes? | A2A: `register-service remoteagent` + A2A server do DevOps Agent. Sub-agentes em qualquer framework. |
| E a integração com GitHub/GitLab? | Nativa (GitHub App ou PAT; GitLab.com e self-hosted). Dá ao agente diff, autor e PRs; habilita release management e skills sincronizadas de repo. Não está nesta demo por restrição da conta usada, não do produto. |

---

## Plano B por etapa

| Se falhar… | Faça |
|---|---|
| Nenhum OOM em 5 min | `aws cloudwatch set-alarm-state --alarm-name checkout-api-container-restarts --state-value ALARM --state-reason demo` |
| Investigação não abre sozinha | Aba A → Backlog → *New investigation* → colar o título `[ALARM] checkout-api-container-restarts`. |
| Quer um segundo incidente ao vivo | Cenário B: `sed -i '' 's#FRAUD_CHECK_URL=$#FRAUD_CHECK_URL=http://10.255.255.1:8443/score#; s/APP_VERSION=1.0.0/APP_VERSION=1.1.0/' overlays/prod/kustomization.yaml` + commit "feat(prod): enable fraud scoring before checkout (v1.1.0)". Latência vai a 3 s em 1 min, alarme p99 em ~3 min. |
| Investigação lenta | Aba E (ensaio de 13/10). Mesma sequência da Parte 4. |
| Kiro CLI sem MCP | `scripts/investigation-tail.py` (mesma API) + PR manual do 5.1. |
| Sem internet | Vídeo de 90 s + repo local. |

## Reset pós-demo

```bash
# volta tudo ao baseline (resources, fraud desligado, versão)
sed -i '' 's/            memory: 48Mi/            memory: 64Mi/; s/            memory: 56Mi/            memory: 128Mi/; s#FRAUD_CHECK_URL=http://10.255.255.1:8443/score#FRAUD_CHECK_URL=#; s/APP_VERSION=1.1.0/APP_VERSION=1.0.0/' overlays/prod/kustomization.yaml
python3 scripts/gh-commit.py hellenavilarosa/checkout-devops-demo main "chore(prod): restore baseline after demo" overlays/prod/kustomization.yaml
kubectl --context $CTX annotate application checkout-api -n argocd argocd.argoproj.io/refresh=hard --overwrite
```

---

## Apêndice A — Conteúdo do Agent Space (já aplicado; fonte em `agent-space/`)

Tudo abaixo já existe no Agent Space, criado por `scripts/apply-agent-space.py`. Os arquivos-fonte são a referência; os textos abaixo são cópia para leitura rápida.

| Asset | Arquivo | Id |
|---|---|---|
| skill `runbook-argocd-rollback` (GENERIC) | `agent-space/skills/runbook-argocd-rollback/SKILL.md` | `ki-5d51d79d-…` |
| skill `skip-canary-window` (INCIDENT_TRIAGE) | `agent-space/skills/skip-canary-window/SKILL.md` | `ki-da532b0f-…` |
| memory store `checkout-directives` | — | `ki-ecd1a491-…` |
| memory `services/checkout-api/deployment-conventions` | `agent-space/memories/checkout-gitops-conventions.md` | `ki-d9e1e6aa-…` |
| custom agent `eks-daily-health` | `agent-space/agents/eks-daily-health/AGENT.md` | `ki-46b44c1a-…` |
| custom agent `checkout-api-specialist` (sob demanda, escopo por tag `project=checkout-api`) | `agent-space/agents/checkout-api-specialist/AGENT.md` | `ki-163df40a-…` |
| trigger diário 11:00 UTC → `custom:ki-46b44c1a-…` | — | `goal-5157eda1-…` |

### Skill `runbook-argocd-rollback`

```markdown
# Runbook: rollback de aplicação gerida por ArgoCD

## Quando usar
Incidente em uma Application do ArgoCD com auto-sync e self-heal ativados, em que a
causa provável é uma mudança recente de configuração ou imagem.

## Regras
1. NUNCA sugerir `kubectl rollout undo`, `kubectl edit` ou `kubectl apply` manual:
   o self-heal reverte a mudança em segundos e esconde a mitigação.
2. A mitigação é sempre um commit no repositório GitOps da Application
   (campo `spec.source.repoURL`), no path do overlay afetado.
3. Preferir revert do commit culpado a um fix manual, salvo quando o revert
   reintroduz outro problema conhecido.

## Passos
1. Identificar a Application (`kubectl get application -n argocd`) e a revisão
   atual (`status.sync.revision`).
2. Comparar a revisão atual com a anterior no histórico do ArgoCD
   (`status.history`) e no Git.
3. Localizar a mudança de ConfigMap/imagem/probe que coincide com o início do erro.
4. Propor: revert do commit (ou patch mínimo) + verificação pós-sync
   (pods Ready, taxa de erro < 1% por 5 min).
5. Registrar follow-ups: mudanças adjacentes que mascararam o problema
   (ex.: probes alteradas) e permissões que impediram leitura completa.

## Verificação
- `kubectl get application <nome> -n argocd` mostra `Synced` e `Healthy`.
- Métrica de 5xx volta ao baseline por 5 minutos.
```

### Custom agent `eks-daily-health`

- **Schedule:** diário, 08:00 America/Sao_Paulo.
- **Instrução:**

```
Você é um SRE revisando a saúde do cluster EKS eks-agentic-troubleshooting (us-east-1)
nas últimas 24 horas. Use Container Insights, CloudWatch Logs e alarmes.

Verifique e reporte, nesta ordem:
1. Pods com mais de 3 restarts, por namespace, com a última razão de término.
2. Nodes com MemoryPressure, DiskPressure ou PIDPressure.
3. PVCs acima de 80% de uso.
4. Alarmes que mudaram de estado (OK<->ALARM), quantas vezes e por quanto tempo.
5. Deployments cujo número de réplicas disponíveis ficou abaixo do desejado por mais de 5 min.

Formato: relatório em Markdown com uma seção por item, uma linha de resumo no topo
("Saudável" / "Atenção" / "Crítico" e por quê), e no máximo 3 recomendações acionáveis.
Se nada for encontrado em um item, escreva "nenhum" — não invente.
```

---

## Apêndice B — Glossário de uma página (reler antes de começar)

| Termo | Em uma frase | Onde aparece na demo |
|---|---|---|
| **Agent Space** | O escopo do agente: contas AWS associadas (via role read-only), integrações, conhecimento (skills, memórias), custom agents, usuários. Um por time/serviço. | Tudo roda dentro do `checkout-devops-demo`. |
| **Investigação** | Unidade de trabalho de incidente: Triage → RCA → Mitigação, com Timeline e Summary. Criada por alarme, ticket, webhook, API, chat ou manualmente. | Parte 2 e 4. |
| **Triage** | Primeiros segundos: correlaciona com investigações abertas, aplica skills de Triage (skip/correlação), decide investigar ou marcar *Skipped*. | 2.2 e 3.3. |
| **Sub-agentes** | Partes da investigação delegadas em paralelo; cada um tem timeline própria expansível. | 4.1 (`logs`, `argocd-change`, `propose-mitigation`). |
| **Timeline / Summary** | Timeline = cada passo e tool call (auditoria). Summary = resumo vivo (leitura rápida). | 4.1. |
| **Propostas de mitigação** | Aparecem inline ao fim da RCA; são plano, não execução. | 4.1. |
| **Chat with investigation** | Mensagem direcionada a uma investigação; steering se rodando, reabre se concluída. | 4.2. |
| **Fork** | Nova investigação que herda a história até uma mensagem e segue outra hipótese. | 4.3. |
| **Review** | Avaliação de qualidade sob demanda: helpful/not helpful + 5 critérios (editáveis). | 4.4. |
| **Feedback** | Veredito estruturado (correto/incorreto + causa real) → métricas de acurácia + memória em `reflections`. | 3.6. |
| **Skill** | `SKILL.md` procedural ("como fazer"), carregada sob demanda pela description; pode ter agent type. Runbook como código. | 3.1 e 3.3. |
| **Learned skills** | Skills que o agente gera ao estudar o ambiente (Agent Space Understanding etc.). | 3.1. |
| **Memória / memory store** | Conhecimento informacional ("o que é verdade aqui"), em stores: `monitors`, `directives`, `reflections`, understanding-*, e os seus. Versionada. | 3.2. |
| **Agent instructions (AGENTS.md)** | Instruções sempre carregadas em toda sessão (diferente de skill, que é sob demanda). Não mostramos; citar se perguntarem "e regras globais?". | Q&A. |
| **Custom agent** | Agente definido por você: system prompt + tools + skills + memory stores + trigger (schedule ou evento). Cada run = invocation com trajetória. | 3.4. |
| **Directed actions** | Ações mutáveis que o agente executa só com (a) preferência ligada no Agent Space, (b) role elevado registrado, (c) aprovação do operador no chat. Desligado por padrão. | Q&A ("ele executa sozinho?"). |
| **MCP remoto / power do Kiro** | Endpoint MCP do DevOps Agent para IDEs e CLIs; no Kiro vem como power. | 5.1. |
| **A2A / ACP / webhooks / API** | Outras portas: agente↔agente, protocolo de cliente, eventos de entrada, API direta. | 5.1 e Q&A. |
| **EventBridge** | Eventos de saída do agente (investigação criada/concluída/skipped) para automação própria. | 2.1 (usamos no sentido inverso: alarme → Lambda → agente). |
| **Release management (preview)** | Lado *change*: release readiness review em PRs/MRs e release testing. Exige repo conectado. | 5.2 (só citado). |
| **Private Connection** | Acesso do agente a ferramentas em rede privada (GitLab self-hosted, Grafana interno) via VPC Lattice. | Q&A. |
| **Recommendations / Backlog** | Além de investigações, o agente gera recomendações proativas priorizadas (prevenção) no Backlog. | Visível na aba Backlog; citar se sobrar tempo. |
