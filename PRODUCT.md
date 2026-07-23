# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

O usuário principal é o operador avançado individual do Hermes Agent que mantém vários profiles e precisa de isolamento real entre o agente pai e determinados subagentes.

Esse usuário escolhe a fronteira de processo por motivos concretos: conter crashes, executar o toolset completo do profile de destino, separar sessões e estado, ou usar uma instalação/versão diferente do Hermes no processo filho. Ele entende o custo de cold start e quer diagnóstico operacional claro quando uma delegação falha.

## Product Purpose

`delegate-profile` adiciona ao Hermes Agent uma ferramenta de delegação cross-profile com uma fronteira real de processo. Seu propósito é executar uma tarefa em outro profile sem compartilhar o processo do agente pai, preservando o ambiente, a sessão, as skills, a memória, o modelo e o toolset próprios do filho.

O produto tem sucesso quando:

- a tarefa chega ao profile escolhido no ambiente Hermes correto;
- uma falha ou crash do filho não derruba o agente pai;
- erros de configuração, timeout e subprocesso são explícitos e acionáveis;
- chamadas para o próprio profile evitam o custo desnecessário de um subprocesso;
- automação de roteamento nunca remove o controle manual do operador.

## Positioning

O plugin existe para o caso em que a fronteira de processo é parte do requisito, não apenas um detalhe de implementação. Diferentemente da delegação cross-profile in-process nativa, ele pode oferecer isolamento de crash, o toolset completo do profile de destino e uma instalação ou versão diferente do Hermes no filho.

O capability router e as interfaces operacionais ampliam essa capacidade, mas não substituem nem redefinem o núcleo: delegação isolada e controlável entre profiles.

## Operating Context

O produto é instalado como plugin do Hermes Agent e registra a tool `delegate_profile` no toolset de delegação. O operador mantém profiles Hermes separados e fornece uma meta autocontida, contexto opcional, profile, modelo e timeout.

Quando o profile de destino difere do profile ativo, o plugin executa um processo Hermes one-shot. Quando um profile explícito coincide com o ativo, a chamada segue pelo `delegate_task` in-process para evitar spawn sem benefício de isolamento.

O operador pode:

- escolher profile e modelo explicitamente;
- omitir o profile ou usar `auto` para acionar o capability router opcional;
- configurar a política do router em `router.yaml`;
- inspecionar decisões e estado por CLI ou por interfaces web auxiliares.

A interface existente do Dashboard Hermes expõe status, simulação de regras, lint, blocklist, log e policy. Outras interfaces, incluindo uma possível superfície no Hermes One, devem permanecer clientes auxiliares do mesmo core e da mesma fonte de política.

## Capabilities and Constraints

- Cross-profile delegation usa um subprocesso Hermes separado e one-shot.
- O processo filho recebe o `HERMES_HOME` efetivo do pai e tem recursão de `delegate_profile` desabilitada.
- Profiles de destino são validados antes do spawn; erros devem listar uma correção ou profiles disponíveis.
- A execução suporta timeout, cancelamento, encerramento da árvore de processos e envelopes JSON estáveis de sucesso ou falha.
- Same-profile explícito usa a delegação in-process nativa; o subprocesso só deve existir quando a fronteira agrega valor.
- Profile explícito preserva controle manual. Profile omitido ou `auto` habilita o router opcional.
- O router avalia regras determinísticas antes de recorrer a um classificador LLM.
- Falhas do router ou classificador não devem impedir a delegação; a política possui fail-safe e fallbacks.
- A política do router é declarativa, editável em YAML e deliberadamente limitada a operadores fechados, sem código arbitrário.
- Blocklist e circuit breaker protegem contra modelos ou rails instáveis sem exigir infraestrutura externa.
- Interfaces operacionais não devem manter uma segunda política nem apresentar simulações como se fossem telemetria de decisões reais.
- O plugin requer Hermes Agent, o toolset de delegação e ao menos um profile adicional para o caso cross-profile.
- O pacote suporta Python 3.11 e 3.12 e é distribuído sob licença MIT.

## Brand Commitments

O nome público atual é `delegate-profile`; o nome do pacote é `hermes-delegate-profile` e a tool principal é `delegate_profile`.

A comunicação é técnica, direta e operacional. Deve explicar claramente quando usar a ferramenta e quando preferir o `delegate_task` nativo, sem vender subprocessos como uma vantagem universal.

Interfaces auxiliares devem preservar a terminologia do core: profile, model, provider/rail, tier, rule, classifier, fail-safe, blocklist, breaker e decision log.

## Evidence on Hand

- `README.md`: contrato público, comparação com `delegate_task`, instalação, uso e formatos de resultado.
- `plugin.yaml` e `__init__.py`: registro e comportamento efetivo da tool e dos hooks.
- `router.yaml`: fonte declarativa da política de roteamento.
- `router/`: implementação do router, classificação, fallbacks, blocklist, breaker, cache e decision log.
- `dashboard/`: interface operacional existente para o Dashboard Hermes.
- `docs/superpowers/specs/2026-07-21-capability-router-design.md`: especificação de arquitetura do capability router.
- `tests/` e `.github/workflows/ci.yml`: suíte automatizada e gate de cobertura do comportamento atual.

Não há no repositório depoimentos, estudos de adoção, métricas de produção ou garantias de desempenho que possam ser apresentados como prova externa.

## Product Principles

1. **Isolamento só quando é real e necessário.** A fronteira de processo deve entregar contenção e ambiente próprio; o caminho same-profile não paga esse custo sem benefício.
2. **Controle manual prevalece sobre automação.** O operador sempre pode fixar profile e modelo; roteamento automático é opt-in por omissão ou `auto`.
3. **Determinismo antes de inferência.** Regras baratas, auditáveis e previsíveis decidem primeiro; o classificador entra apenas quando necessário.
4. **Falhar de forma segura e acionável.** Router, provider, subprocesso ou configuração podem falhar sem derrubar o pai e sem esconder a causa.
5. **Uma única autoridade operacional.** CLI e interfaces web observam e operam o mesmo core e a mesma policy; não duplicam decisões nem fabricam telemetria.
