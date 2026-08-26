# Migração de nomes — extensões e integrações Hermes

**Status:** origens das extensões consolidadas; aguardando reconciliação do runtime `delegate-profile` antes da Fase A
**Escopo:** `delegate-profile`, extensões do Hermes One e seus sidecars locais.
**Fora de escopo:** renomear profiles Hermes, tool names já expostos ou alterar
portas/hosts de serviços.

## 0. Origens Git consolidadas

| Componente | Origem | Commit inicial publicado | Estado |
|---|---|---|---|
| Bundle de extensões Hermes One | `https://github.com/rodrigogs/hermes-one-extensions` | `f2866eb` | `main` rastreia `origin/main` |
| Fact Explorer | `https://github.com/rodrigogs/hermes-one-fact-explorer` | `aa0ee35` | `main` rastreia `origin/main` |
| Router/delegação | `https://github.com/rodrigogs/hermes-delegate-profile` | existente | runtime ainda precisa ser reconciliado |

Os dois primeiros repositórios foram publicados após snapshot local, revisão de
diff, scan de credenciais de alta confiança e execução das suítes focadas. O
antigo `assistant-avatar`, que já não constava no manifest nem tinha referências
ativas, foi removido explicitamente do bundle.

## 1. Princípios

1. O ID precisa nomear uma responsabilidade, não um detalhe acidental da
   implementação.
2. IDs de extensões Hermes One usam o prefixo `hermes-one-`: eles coexistem no
   manifest local, podem aparecer em URLs de proxy e não devem competir por um
   nome genérico com projetos de terceiros.
3. Labels para pessoas são curtos; IDs para máquinas são explícitos.
4. Um ID de extensão que tem sidecar é um contrato: identifica manifest,
   diretório, URL `/api/extensions/<id>/sidecar`, estado de consentimento e
   token `token-v1`.
5. Cada migração deve manter o runtime saudável por si só. Não agrupar mudanças
   de sidecar com mudanças puramente visuais.

## 2. Identidades finais

| Camada | Atual | Final | Label humano | Decisão |
|---|---|---|---|---|
| Plugin Agent | `delegate-profile` | `delegate-profile` | Delegate Profile | **manter** |
| Tool Agent | `delegate_profile` | `delegate_profile` | — | **manter** |
| Biblioteca de extensões One | `hermes-one-extension-kit` | `hermes-one-extension-kit` | Hermes One Extension Kit | **concluído** |
| Painel Office | `hermes-one-office-3d` | `hermes-one-office-3d` | Office 3D | **concluído** |
| Painel/sidecar de roteamento | `hermes-one-capability-router` | `hermes-smart-router` | Smart Router | **concluído — Fase E, decisão do operador 2026-08-26** |
| Painel/sidecar de memória | `hermes-one-fact-explorer` | `hermes-one-fact-explorer` | Fact Explorer | **concluído** |
| Unit Router | `hermes-router-sidecar.service` | manter inicialmente | Profile Router sidecar | label agora; ID depois |
| Unit Memory | `hermes-memory-sidecar.service` | manter inicialmente | Fact Explorer sidecar | label agora; ID depois |

### Justificativas

- `delegate-profile` já é um nome preciso para a API que cria uma fronteira de
  processo entre profiles. Renomeá-lo quebraria o tool name, configuração,
  estado e distribuição sem corrigir ambiguidade material.
- `hermes-panel` não é um painel: fornece navegação, ciclo de vida de views e
  bridge de tema para as demais extensões. `hermes-one-extension-kit` descreve
  essa função e não disputa o nome genérico `hermes-panel`.
- O Office não é mais um launcher separado; é uma view persistente no shell.
- `Profile Router` foi **rejeitado pela evidência**. A justificativa original dizia
  que o router decide o profile alvo; medido sobre 252 decisões reais em
  `state/routes.jsonl`, o profile é praticamente constante (`coder` em 230, mais 22
  vetos de blocklist sem profile) enquanto 7 modelos distintos foram escolhidos. A
  variável que este componente decide é o modelo, por capacidade: as regras
  produzem `model` além de `profile`, a tabela de política é `tiers: T1..T4` com
  `_TIER_ORDER`, o piso de sessão compara tiers de capacidade, `service.routes()`
  expõe `model` e não `profile`, e o console lê `model` 95 vezes, `tier` 27 e
  `profile` 3. `Profile Router` apontaria justamente para a dimensão que não varia,
  e brigaria com o vocabulário do próprio console — uma view "Profile Router" cuja
  seção principal é "Capability ladder". O prefixo do bundle resolve a colisão de
  namespace sem trocar a superfície: `hermes-one-capability-router`.
- A memória apresenta fatos, confiança e alcance; o grafo é somente uma das
  visualizações. `Fact Explorer` descreve a experiência real — e ao contrário do
  caso do router, a medição **confirma** a justificativa. O schema tem `facts`
  (121, com FTS e embeddings), `entities` (396) e `fact_entities` (659), e nenhuma
  tabela de grafo: `graph.py` diz no próprio docstring que "the store has no
  fact→fact edges" e deriva as arestas por request. No console, `#modeList` traz
  `aria-selected="true"` e `#graphPane` traz `hidden`; o texto lê `fact` 181 vezes
  contra `graph` 88. A lista é a interface primária, o grafo é o segundo modo.
  O label do rail era `Graph` e passou a `Facts`, que é o que o painel abre.

## 3. Compatibilidade que cada ID exige

| Contrato | Kit | Office | Profile Router | Fact Explorer |
|---|---:|---:|---:|---:|
| `extensions.json` | sim | sim | sim | sim |
| diretório de assets | sim | sim | sim | sim |
| referências JavaScript/CSS | sim | sim | sim | sim |
| ordem de carregamento | sim | sim | sim | sim |
| URL sidecar | não | não | sim | sim |
| consentimento persistido | não | não | sim | sim |
| token `token-v1` | não | não | sim | sim |
| instalador/updater | sim | sim | sim | sim |
| unidade systemd | não | não | label; ID posterior | label; ID posterior |

## 4. Ordem de execução

### Fase A — Extension Kit

Renomear o diretório e ID `hermes-panel` para `hermes-one-extension-kit` de
forma atômica no manifest e nos três consumidores. É a menor mudança de risco:
não possui sidecar, token nem estado de consentimento.

**Aceite:** todos os três painéis continuam montando, a navegação nativa do
Hermes One continua coerente em desktop/mobile e os testes JavaScript passam.

### Fase B — Office 3D

Concluída. `office-3d-launcher` renomeado para `hermes-one-office-3d`; a
funcionalidade e a URL `/office/` não mudaram. A classe de navegação acompanhou o
ID (`office-3d-nav` para `hermes-one-office-3d-nav`), seguindo a convenção dos
irmãos, e `REQUIRED_EXTENSION_IDS` no updater passou a nomear os IDs pós-rename —
ele ainda exigia `hermes-panel` e falharia sobre um manifest saudável.

**Aceite:** o Office abre no painel, preserva iframe/câmera ao alternar de
view e não recarrega ao retornar.

### Fase C — Profile Router

Concluída, com o nome mantido: `capability-router` recebeu apenas o prefixo do
bundle e virou `hermes-one-capability-router` em manifest, assets, instalador,
sidecar, console, testes, CI e updater — 53 referências em dois repositórios. A
`navClass` acompanhou o ID, como nas fases A e B.

Consentimento/token: **nada a migrar, e verificado antes de mexer**. O token vive
em `~/.hermes/webui/sidecar-auth/<extension-id>.token` e o diretório não existe em
nenhuma das duas instalações; `read_expected_token()` retorna `present=False` sob
o ID antigo e sob o novo. O console nunca foi consentido aqui — `/routes` responde
`sidecar token not provisioned`, e `/health` é auth-exempt, que é por que ele
respondia mesmo assim. Não houve 403 para silenciar: no primeiro consentimento a
WebUI provisiona o token já sob o ID novo.

O nome do arquivo da unit segue como `hermes-router-sidecar.service`, conforme a
regra desta fase; só a descrição muda.

A unit fica com o nome atual na primeira release, mas recebe a descrição
`Hermes One Profile Router sidecar`. A renomeação da unit é uma operação
separada com alias e rollback.

**Aceite:** status de extensão, proxy consentido, `/health`, console do Router
e `delegate_profile` funcionam depois da migração.

### Fase D — Fact Explorer e origem versionada

Concluída. A precondição já estava satisfeita: o código tem origem versionada em
`rodrigogs/hermes-one-fact-explorer`, limpa e em sync. Segue não sendo plugin Agent
— não possui `plugin.yaml` — e isso não muda.

Renomeados o ID, o diretório de assets do console, o diretório do plugin em disco,
o `EXTENSION_ID` do sidecar (com o caminho do token e do console derivando dele), o
manifest e o exemplo rastreado, os testes e o README — 18 referências no repo do
Fact Explorer e 16 no bundle de extensões.

O label do rail era `Graph` e o título `Memory graph`; passaram a `Facts` e
`Fact Explorer`, porque o grafo é o segundo modo e não o primeiro.


A unit mantém o ID de arquivo atual nesta fase e passa a ter descrição
`Hermes One Fact Explorer sidecar`.

**Aceite:** console read-only abre por proxy, facts/lista/pesquisa/grafo
funcionam, não há escrita no store e o updater inclui a nova origem.

### Fase E — Smart Router (2026-08-26)

Decisão do operador, registrada sem discutir: o id `hermes-one-capability-router`
passa a `hermes-smart-router` e o label visível passa a **Smart Router**. A
convenção do pack (`hermes-one-*`) e a precisão descritiva de "capability" foram
pesadas e rejeitadas; a escolha é do operador.

Renomeados no repo: o diretório de assets, o `EXTENSION_ID` do sidecar (o caminho
do token e o do console derivam dele), o manifest (`id` e `name`), o instalador,
a `navClass` e os títulos visíveis (rail title, sidebar head, iframe title, card
do dashboard, `Description=` das units, descrição da CLI). Specs datadas em
`docs/superpowers/specs/` mantêm o nome da época — são histórico, não contrato
vigente.

O instalador ganhou `_RETIRED_EXTENSION_IDS`: o merge do manifesto casa pelo id
CORRENTE, então a entrada sob o id antigo sobreviveria a todo install — dois
botões na rail, um apontando para um consentimento que não existe. O sweep tira
a entrada do `extensions.json` E o diretório de assets do disco.

A unit **não** foi renomeada (`hermes-router-sidecar.service` mantém o nome de
arquivo): renomear deixaria a unit antiga enabled segurando a porta 8791 enquanto
a nova falhava com "address already in use". Só a `Description=` mudou.

Migração de estado (token e consentimento são indexados pelo id): o token
`~/.hermes/webui/sidecar-auth/hermes-smart-router.token` é cunhado pela WebUI no
ato de conceder o consentimento; o consentimento antigo sob o id aposentado foi
removido do `extension-overrides.json` e o novo concedido em Settings →
Extensions. O token antigo foi removido junto — um arquivo de credencial sem
consumidor não fica no disco.

## 5. Reversão

Cada fase produz um commit independente. Em caso de falha, reverter apenas o
commit da fase correspondente e reiniciar somente os serviços afetados. As
fases A e B não exigem restart de sidecar; C e D exigem healthcheck antes de
considerar a troca concluída.

## 6. Pesquisa de colisões

As buscas públicas em 2026-07-27 encontraram colisões relevantes para os nomes
atuais `hermes-panel`, `capability-router` e `memory-graph`. Não retornaram
repositórios GitHub para os IDs finais acima no momento da consulta. Isso não
constitui registro de marca; é uma verificação prática de namespace e
confusão operacional.
