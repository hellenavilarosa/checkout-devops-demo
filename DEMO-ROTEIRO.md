# Roteiro — AWS DevOps Agent para um time DevOps/SRE sênior (EKS + ArgoCD)

**Duração:** 60 min (45 demo + 15 discussão) · **Formato:** tudo ao vivo, 1 slide no fechamento

**Mensagem central:** o DevOps Agent não é um chatbot em cima do CloudWatch. É um colega de on-call que vocês **programam** (skills, memórias, regras, agentes agendados), que **fecha o loop** (do alarme ao PR) e que **se integra ao que vocês já têm** (ArgoCD, GitHub, Slack, Kiro, MCP/A2A).

Como ler este roteiro: cada passo tem **Onde** (qual aba/terminal), **Faça**, **Diga** e, quando houver, **Cole no chat** (texto pronto para o DevOps Agent ou Kiro CLI). Os tempos somam 45 min.

---

## Parte 0 — O que foi montado e por quê (leia antes; não é apresentado)

| Componente | O que é | Por que está na demo |
|---|---|---|
| **App `checkout-api`** (ns `checkout`) | API Python com `/checkout` (usa Redis), `/health`, `/ready`, `/metrics`. Logs em JSON. 2 réplicas + `redis` + `loadgen` (~5 req/s). | App limpa e realista. Toda a falha é injetada via Git, nunca via `kubectl`. |
| **Repo GitOps** `checkout-devops-demo` | Kustomize: `base/` (manifests + código) e `overlays/prod/` (ConfigMap com `REDIS_HOST`, `APP_VERSION`). | Espelha o fluxo do cliente: tudo entra por Git. |
| **ArgoCD Application `checkout-api`** | Auto-sync + self-heal + prune, aponta pro repo acima. | Quando o commit ruim entra, o ArgoCD aplica sozinho. Self-heal também impede `kubectl rollout undo` — ponto de discussão. |
| **Add-on `amazon-cloudwatch-observability`** | Container Insights + Fluent Bit mandando logs dos pods pro CloudWatch (`/aws/containerinsights/<cluster>/application`). | É daqui que o DevOps Agent lê métricas e logs. Sem isso ele não vê o cluster. |
| **Metric filters + alarme `checkout-api-error-rate`** | Filtros contam requests e 5xx nos logs; metric math `100*5xx/requests > 20%` por 2 min. | Detecção. É o gatilho de tudo. |
| **EventBridge → Lambda `checkout-demo-alarm-to-investigation`** | Regra escuta mudança de estado do alarme → Lambda chama `CreateBacklogTask(INVESTIGATION)` no Agent Space. ~50 linhas, idempotente. | Faz a investigação abrir **sozinha**. Pra quem usa PagerDuty/Datadog/Grafana a integração é nativa e nem precisa da Lambda. |
| **Agent Space `checkout-devops-demo`** | Escopo do agente: conta associada via role `DevOpsAgentRole-AgentSpace` (read-only, `AIDevOpsAgentAccessPolicy`). | Onde moram investigações, skills, memórias, regras, custom agents. |
| **`scripts/gh-commit.py`** | Commita no GitHub pela API REST (sem `git push`, bloqueado nesta máquina). | É o "git push" da demo. |
| **`scripts/investigation-tail.py`** | Tail da investigação no terminal via API do DevOps Agent. | Mostra que tudo que a UI faz é API — e serve de fallback se a UI demorar. |

Ensaio de 13/10: alarme às 15:12 UTC → investigação criada em segundos → root cause às 15:18 → plano de mitigação às 15:23 (~9 min). O agente achou a causa (`REDIS_HOST=redis-cache`), uma **segunda mudança** que mascarou o rollout (readiness probe `/health`→`/ready`) e declarou o que não conseguiu ler por RBAC (`syncPolicy` da Application).

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

Abas do navegador, nesta ordem: **(A)** web app do DevOps Agent · **(B)** GitHub repo · **(C)** ArgoCD UI (`kubectl --context $CTX port-forward -n argocd svc/argocd-server 8080:443`) · **(D)** CloudWatch → alarme `checkout-api-error-rate` · **(E)** investigação do ensaio de 13/10 já aberta (fallback).

Checklist:

- [ ] `overlays/prod/kustomization.yaml` saudável: `REDIS_HOST=redis`, `APP_VERSION=1.0.0`. Alarme `OK`. T2 mostrando `200`.
- [ ] Web app logado no Agent Space `checkout-devops-demo`.
- [ ] **Sem integração GitHub no Agent Space** (restrição da conta interna). A demo não depende dela: o agente acha a causa pelo ConfigMap e pelo histórico do ArgoCD. Se perguntarem, resposta pronta na tabela do fechamento.
- [ ] Skill `runbook-argocd-rollback` e custom agent `eks-daily-health` criados (textos no Apêndice A).
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

**Diga:** "Tudo isso entra por Git. Igual a vocês. Repare no ConfigMap: `REDIS_HOST=redis`."

### 1.3 Quebrar via Git (2 min) — Onde: T3

**Faça:**

```bash
sed -i '' 's/REDIS_HOST=redis$/REDIS_HOST=redis-cache/; s/APP_VERSION=1.0.0/APP_VERSION=1.1.0/' overlays/prod/kustomization.yaml
grep -nE 'REDIS_HOST|APP_VERSION' overlays/prod/kustomization.yaml
python3 scripts/gh-commit.py hellenavilarosa/checkout-devops-demo main \
  "chore(prod): point checkout-api at the new redis-cache service (v1.1.0)" overlays/prod/kustomization.yaml
kubectl --context $CTX annotate application checkout-api -n argocd argocd.argoproj.io/refresh=hard --overwrite
```

**Diga:** "Mensagem de commit plausível — alguém achou que o Redis tinha sido renomeado. Passou no review." Em ~30 s T1 mostra rollout e T2 vira `503`. "Pod `1/1 Running`. Pra o Kubernetes está tudo bem. Pra o usuário, não."

---

## Parte 2 — Detecção e início da investigação (5 min)

### 2.1 Enquanto o alarme arma (3 min) — Onde: aba D, depois console EventBridge/Lambda

O alarme leva ~2–3 min (janela de 2 min + propagação dos logs). Use esse tempo para mostrar o gatilho **no console**:

**Faça:** CloudWatch → alarme → aba *Details* (metric math). Depois Logs → metric filters do log group `application`. Depois EventBridge → regra `checkout-demo-alarm-to-investigation` → target Lambda.

**Diga:** "Logs dos pods vão pro CloudWatch via Container Insights. Dois metric filters contam requests e 5xx; a taxa passa de 20% por 2 minutos, o alarme dispara, o EventBridge chama uma Lambda de 50 linhas que abre uma investigação no agente. Isso é o caminho genérico. Se vocês usam PagerDuty, Datadog, Grafana, Dynatrace ou ServiceNow, a integração é nativa — não precisa nem da Lambda."

**Plano B:** se passar de 3 min: `aws cloudwatch set-alarm-state --alarm-name checkout-api-error-rate --state-value ALARM --state-reason demo` (dispara o EventBridge de verdade).

### 2.2 A investigação abre sozinha (2 min) — Onde: aba A → Backlog

**Faça:** mostrar a task `[ALARM] checkout-api-error-rate` aparecendo em *Backlog* (`PENDING_START` → `IN_PROGRESS`). Abrir. Mostrar as primeiras mensagens da timeline: leitura do alarme, hipóteses, subagentes `logs` / `argocd-change` / `propose-mitigation` partindo em paralelo.

**Diga:** "Ninguém clicou em nada. O agente já está fazendo o que o on-call faz nos primeiros 5 minutos: lendo o alarme, puxando métricas, notando que o erro começou junto com um deploy. Ele leva uns 9 minutos até o plano. Enquanto ele trabalha, eu mostro como esse agente é programado — e volto pra investigação quando ela terminar."

Em T3, deixar rodando pra espiar entre passos:

```bash
python3 scripts/investigation-tail.py --n 8
```

---

## Parte 3 — Programando o agente, enquanto ele investiga (12 min)

Tudo na aba A. Diga uma vez: "Agora a parte que muda a relação de vocês com a ferramenta."

### 3.1 Skills em Git (4 min) — Onde: Knowledge → Skills

**Faça:** abrir `runbook-argocd-rollback` (pré-criada). Mostrar que é Markdown: quando aplicar, passos, o que NÃO fazer.

**Diga:** "Isto é o runbook de vocês virando comportamento do agente. É Markdown, então vocês versionam no repo de vocês e sincronizam com o Agent Space — mudou o runbook, mudou o agente, com PR, review e blame. Não é prompt solto."

Abrir também a skill gerada pelo próprio agente (task `SYSTEM_LEARNING` no backlog, ex. *Agent Space Understanding*). **Diga:** "Ele estudou o ambiente antes do primeiro incidente."

### 3.2 Memórias (2 min) — Onde: Knowledge → Memories

**Faça:** mostrar as memórias existentes. Adicionar uma ao vivo.

**Cole:**
> Deploys do checkout-api (namespace checkout) acontecem exclusivamente via ArgoCD com self-heal e prune ativados. Mudanças manuais no cluster são revertidas automaticamente; a mitigação correta é sempre um commit no repo checkout-devops-demo.

**Diga:** "Na próxima investigação ele não redescobre isso — e não sugere `kubectl rollout undo`."

### 3.3 Regras de incident-skip (2 min) — Onde: Settings → Incident rules (ou equivalente)

**Faça:** criar ao vivo.

**Cole:**
> Não abrir investigação para o alarme checkout-api-error-rate quando ele disparar até 10 minutos após um sync do ArgoCD da Application checkout-api cuja revisão tenha a anotação canary=true.

**Diga:** "Vocês controlam o ruído. Deploy em janela conhecida não vira incidente."

### 3.4 Custom agent agendado (3 min) — Onde: Agents → Custom agents

**Faça:** abrir `eks-daily-health` (pré-criado). Mostrar schedule e instrução. Clicar **Run now** e deixar em background.

**Diga:** "Um SRE agendado. Todo dia 8h ele revisa o cluster e entrega o relatório no Slack. Outros exemplos: queries lentas do RDS da semana, HPAs que nunca escalaram, certificados vencendo. Vocês escrevem a instrução; o agente tem as ferramentas."

### 3.5 Espiar a investigação (1 min) — Onde: T3

```bash
python3 scripts/investigation-tail.py --n 8
```

**Diga:** o que já apareceu ("ele já achou o ConfigMap", etc.). Se ainda não concluiu, continue no 3.6; senão pule pra Parte 4.

### 3.6 Qualidade e labeling (1 min, buffer) — Onde: Quality (ou Insights)

**Faça:** mostrar o dashboard de tarefas e o labeling 👍/👎 com motivo. Rotular a investigação do ensaio.

**Diga:** "Vocês medem a acurácia com os incidentes de vocês, não com o marketing da AWS."

---

## Parte 4 — A investigação concluída (10 min)

Volte à investigação aberta na aba A. Se ainda não terminou, use a aba E (ensaio de 13/10) — a sequência é idêntica.

### 4.1 Ler a timeline (4 min)

Apontar, nesta ordem:

1. **Root cause** — ConfigMap `REDIS_HOST: redis-cache`; confirma que não existe Service `redis-cache` no ns `checkout`; 100% dos erros são `Name does not resolve`.
2. **O achado inesperado** (não pular): uma **segunda mudança** — readiness probe de `/health` para `/ready` — foi o que deixou o pod ruim ficar `Ready` e receber tráfego. **Diga:** "Ninguém pediu isso. Ele correlacionou dois commits e explicou por que o rollout passou."
3. **O que ele não conseguiu** — não leu o `syncPolicy` da Application por RBAC, e disse isso. **Diga:** "Ele declara os limites em vez de inventar. Pra confiar num agente em produção isso importa mais que acertar."
4. **Plano de mitigação** — reverter `REDIS_HOST` via commit; por que `kubectl rollout undo` não resolve (self-heal); follow-up da probe.
5. **Como ele chegou no commit sem ler o GitHub** — pelo histórico de sync do ArgoCD (`status.history`, revisão anterior vs. atual) e pelo valor antigo do ConfigMap. **Diga:** "Com o GitHub conectado ele ainda traz o diff e o autor; aqui ele chegou lá pelo ArgoCD."

### 4.2 Chat na investigação (2 min) — Onde: campo de chat da investigação

**Cole, um por vez:**
> Quantas requests de checkout falharam desde o início do incidente e qual seria o impacto estimado se ele durasse 1 hora?

> Por que o Kubernetes mostrava o pod como Running e Ready se ele não conseguia falar com o Redis?

**Diga:** "Mesmo chat funciona no Slack, uma thread por investigação."

### 4.3 Fork (2 min) — Onde: timeline → mensagem onde ele decide investigar o ConfigMap → *Fork*

**Cole na fork:**
> Investigue a hipótese alternativa: falha de resolução DNS no CoreDNS do cluster afetando todos os pods, e não apenas uma configuração errada do checkout-api. Compare com a evidência já coletada.

**Diga:** "A fork parte do mesmo contexto até aquele ponto e roda em paralelo. Vocês discordam de uma hipótese sem jogar fora o trabalho."

### 4.4 Review (2 min) — Onde: investigação concluída → *Review* (use a aba E se a atual ainda estiver rodando)

**Faça:** lançar o Review.

**Diga:** "O agente avalia a qualidade da própria investigação: evidência suficiente? hipóteses descartadas corretamente? É isso que alimenta o dashboard de qualidade de vocês."

---

## Parte 5 — Fechando o loop (10 min)

### 5.1 Kiro CLI consumindo o DevOps Agent (5 min) — Onde: T4

**Cole no Kiro CLI:**
> Use o DevOps Agent para listar as investigações do Agent Space checkout-devops-demo e me traga o plano de mitigação da mais recente, com a causa raiz em uma frase.

Mostrar o plano chegando via MCP. Depois:

**Cole no Kiro CLI:**
> Aplique a mitigação neste repositório: corrija `overlays/prod/kustomization.yaml` conforme o plano, mantendo APP_VERSION. Crie o branch fix/redis-host, commite com `python3 scripts/gh-commit.py hellenavilarosa/checkout-devops-demo fix/redis-host "<mensagem>" overlays/prod/kustomization.yaml` e abra um PR com `gh pr create` cuja descrição referencie a investigação e resuma a causa.

**Diga:** "O mesmo agente que investigou está sendo consumido de dentro da ferramenta do dev, headless, via MCP. Dá pra fazer isso de um pipeline, de um bot do Slack, de qualquer agente de vocês via A2A."

**Plano B:** fazer em T3:

```bash
sed -i '' 's/REDIS_HOST=redis-cache/REDIS_HOST=redis/' overlays/prod/kustomization.yaml
python3 scripts/gh-commit.py hellenavilarosa/checkout-devops-demo fix/redis-host "fix(prod): restore REDIS_HOST=redis" overlays/prod/kustomization.yaml
gh pr create --repo hellenavilarosa/checkout-devops-demo --head fix/redis-host --title "fix(prod): restore REDIS_HOST=redis" --body "Mitigação da investigação [ALARM] checkout-api-error-rate."
```

### 5.2 O PR (2 min) — Onde: aba B

**Faça:** abrir o PR. Mostrar a descrição referenciando a investigação e o diff de uma linha.

**Diga:** "O lado *change* do agente — release management, em preview — entra aqui: com o repo conectado ele avalia o risco do PR antes do merge, lê dependências e testes. Hoje não vou mostrar porque esta conta não tem a integração habilitada; é o próximo passo natural depois do que vocês viram."

### 5.3 Merge → ArgoCD → verde (3 min) — Onde: T3, T1, T2

```bash
gh pr merge --repo hellenavilarosa/checkout-devops-demo fix/redis-host --squash --delete-branch
kubectl --context $CTX annotate application checkout-api -n argocd argocd.argoproj.io/refresh=hard --overwrite
```

T1: rollout. T2: `200`. Alarme volta a `OK` em ~2 min (mostrar na discussão).

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
1. "Investigou em 9 minutos, achou a causa e um fator contribuinte que ninguém pediu."
2. "Vocês programam o comportamento com runbooks, memórias e regras — não com prompt."
3. "Pode estar incluso no plano de Support de vocês." *(confirmar o tier antes)*

Durante a discussão: mostrar o resultado do `eks-daily-health` (Run now da Parte 3) e o alarme em `OK`.

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
| Alarme não dispara em 3 min | `aws cloudwatch set-alarm-state --alarm-name checkout-api-error-rate --state-value ALARM --state-reason demo` |
| Investigação não abre sozinha | Aba A → Backlog → *New investigation* → colar o título `[ALARM] checkout-api-error-rate`. |
| Investigação lenta | Aba E (ensaio de 13/10). Mesma sequência da Parte 4. |
| Kiro CLI sem MCP | `scripts/investigation-tail.py` (mesma API) + PR manual do 5.1. |
| Sem internet | Vídeo de 90 s + repo local. |

## Reset pós-demo

```bash
# se o PR não foi mergeado
sed -i '' 's/REDIS_HOST=redis-cache/REDIS_HOST=redis/; s/APP_VERSION=1.1.0/APP_VERSION=1.0.0/' overlays/prod/kustomization.yaml
python3 scripts/gh-commit.py hellenavilarosa/checkout-devops-demo main "chore(prod): restore REDIS_HOST=redis" overlays/prod/kustomization.yaml
kubectl --context $CTX annotate application checkout-api -n argocd argocd.argoproj.io/refresh=hard --overwrite
```

---

## Apêndice A — Textos para criar no pré-flight

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
