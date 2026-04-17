# EcoZap — Contexto para Claude Code
> Lido automaticamente pelo terminal Claude Code ao entrar nesta pasta.
> Atualizado a cada sprint. Última atualização: 2026-04-16 | Commit: `8018111`

---

## O QUE É ESTE PROJETO

**EcoZap** — SaaS multi-tenant de atendimento via WhatsApp com força de vendas autônoma.
Um time de agentes de IA trabalha em paralelo: atende leads, qualifica, fecha vendas,
retém clientes, aprende todo dia e se auto-corrige quando algo dá errado.

- **Stack:** FastAPI + Celery + Redis (Railway) | Supabase | Claude Sonnet/Haiku | Evolution API
- **Repo:** `Jhowsilva2k22/whatsapp-agent` (renomear para `ecozap-api`)
- **Deploy:** Railway prod | **Alertas:** Telegram (CEO)

---

## ESTADO ATUAL — Sprint 2 concluído ✅

### O que está funcionando em produção
- Sentinel → Doctor → Surgeon (pipeline de auto-correção a cada 5 min via Celery)
- Guardian (valida backups antes de salvar no Supabase)
- CEO Override via Telegram (APROVADO:id / REJEITADO:id)
- SDR + Closer + Consultant com roteamento automático via AgentService
- QualifierAgent com prompt humanizado (NUNCA INVENTA, NUNCA REVELA)
- Knowledge Bank — banco de conhecimento treinável por owner
- Nightly Learning → alimenta Knowledge Bank automaticamente
- Trainer — owner treina o bot via WhatsApp (/treinar /conhecimento /esquecer)

### Commits importantes
| Commit | O que fez |
|---|---|
| `b3fec71` | Criou `app/agents/__init__.py` e registrou todos os agentes |
| `6b20125` | Sentinel + Doctor + Surgeon implementados com lógica real |
| `cfd1de1` | Notificações Telegram em português para leigos |
| `30d935e` | Guardian integrado ao backup + endpoint council/meeting |
| `0a460af` | SDR + Closer + Consultant + AgentService criados |
| `8018111` | Knowledge Bank + Trainer + SDR relacional + Nightly Learning → KB |

---

## PRÓXIMO SPRINT (Sprint 3)

1. `app/api/webhook.py` — quando owner manda `/treinar`, `/conhecimento`, `/esquecer` → rotear para Trainer (não QualifierAgent)
2. Teste end-to-end: lead entra → SDR → Closer → Consultant
3. Smoke tests: Sentinel, Guardian, Knowledge Bank
4. **Ação manual pendente:** aplicar migration `knowledge_items` no Supabase (ver `scripts/supabase_schema.sql`)

---

## ARQUIVOS CRÍTICOS (leia antes de mexer)

```
app/agents/qualifier.py          Motor do atendente humanizado
app/agents/business/sdr.py       Qualificação relacional + temperatura ❄🌡🔥
app/agents/business/closer.py    Objeções/compra + fechamento
app/agents/business/consultant.py  Retenção + upsell + onboarding
app/agents/business/trainer.py   Treinamento via WhatsApp
app/services/knowledge.py        Banco de conhecimento estruturado
app/services/agent.py            Roteador central SDR/Closer/Consultant
app/services/learning.py         Análise noturna → alimenta KB
app/agents/ops/sentinel.py       Monitoramento de saúde (4 checks)
app/agents/ops/doctor.py         Diagnóstico com 10 padrões de erro
app/agents/ops/surgeon.py        Correção automática via PR GitHub
app/agents/loyalty.py            CEO Override + mensagens humanas Telegram
app/queues/tasks.py              Celery tasks + beat schedule
scripts/supabase_schema.sql      Schema completo (incluindo knowledge_items)
```

---

## ARQUITETURA EM 30 SEGUNDOS

```
WhatsApp → webhook.py → AgentService
   ├── score 0-49   → SDR  (❄ conquista, 🌡 educa)
   ├── score 50+    → Closer (🔥 facilita o fechamento)
   └── status=cliente → Consultant (retém, faz upsell)
          ↓
   QualifierAgent (motor de resposta)
   ├── KnowledgeBank.get_context_for_prompt()  ← sabe o que o dono treinou
   └── build_qualifier_prompt()  ← identidade + temperatura + conhecimento
          ↓
   Celery Beat (5 min): Sentinel → Doctor → Surgeon → PR → CEO aprova
```

---

## REGRAS QUE NUNCA MUDAM (não quebre estas)

1. **NUNCA INVENTA** — se não sabe, diz "deixa eu verificar"
2. **NUNCA REVELA** — nunca confirma ser IA, sistema, backend ou bastidores
3. **SDR nunca empurra venda** — relacionamento primeiro, venda é consequência
4. **CEO Override** — toda ação crítica (merge, deploy, delete) passa pelo Telegram
5. **Knowledge Bank** — memória permanente; tudo que aprende vai para lá
6. **Checkpoint** — atualizar `CLAUDE.md` + `CHECKPOINT.md` + Notion a cada sprint

---

## PENDÊNCIAS MANUAIS (sem código)

| Ação | Onde |
|---|---|
| Aplicar migration `knowledge_items` | Supabase → SQL Editor → cole `scripts/supabase_schema.sql` |
| Renomear repo `whatsapp-agent` → `ecozap-api` | GitHub → Settings |
| Renomear projeto Supabase `axtor_comercial 1` → `ecozap` | Supabase → Settings |

---

## COMO RETOMAR QUALQUER SESSÃO

Se você é uma nova instância do Claude lendo isto:
1. Leia este arquivo — você já sabe tudo do projeto
2. Rode `git log --oneline -10` para ver os últimos commits
3. O próximo passo está na seção **PRÓXIMO SPRINT** acima
4. Nunca reescreva o que já está feito — só avance
