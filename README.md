# miltonjose-cadernostecnicos

Cadernos Técnicos de Composições do **SINAPI** (Caixa) convertidos em JSON e mantidos atualizados por um robô, para uso em todas as ferramentas do miltonjose.com (Quantify, skills Pilar, Caderno de Encargos).

## O que tem aqui

- `dados/cadernos-json/{caderno}.json` — para cada composição: caderno, PDF de origem, página inicial e as seções 3 a 9 do caderno (itens e características, equipamentos, critério de quantificação, critério de aferição, execução etc.), mais o link do PDF, a data do PDF e a base SINAPI usada.
- `dados/cadernos-json/INDEX.json` — composição → caderno, e caderno → arquivo/composições (mesmo formato usado pelo Quantify).
- `dados/estado.json` — versão (tamanho e data) de cada PDF já processado.

## Como usar

```
https://raw.githubusercontent.com/engmiltonjose/miltonjose-cadernostecnicos/main/dados/cadernos-json/INDEX.json
https://cdn.jsdelivr.net/gh/engmiltonjose/miltonjose-cadernostecnicos@main/dados/cadernos-json/INDEX.json
```

## Funcionamento

- Roda **toda segunda-feira às 07:00** (Brasília) e quando chamado em *Actions → Run workflow*.
- Lista os PDFs na página de downloads da Caixa, compara tamanho e data com o estado anterior e reprocessa **só** o que mudou — ou os cadernos cujas composições mudaram na base SINAPI mais recente (lida do robô [miltonjose-sinapi](https://github.com/engmiltonjose/miltonjose-sinapi)).
- Abre uma *issue* (e-mail) quando atualiza algo ou quando falha.

Fonte: Caixa Econômica Federal — SINAPI, Cadernos Técnicos de Composições.
