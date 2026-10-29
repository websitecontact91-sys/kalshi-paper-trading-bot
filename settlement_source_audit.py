"""Inspect Kalshi's own rule/settlement metadata for discovered 15-minute series.

Read-only research. No price source is enabled by this report.
"""
import json
from bot import Kalshi
from discover import BASE, discover_series


def main():
    client = Kalshi(BASE)
    names, catalog = discover_series(client, max_pages=50)
    output = {'catalog': catalog, 'series': []}
    for name in names:
        item = {'ticker': name}
        try:
            data = client.get('/series/' + name)
            series = data.get('series') or {}
            for field in ('title', 'category', 'contract_terms_url',
                          'rules_primary', 'rules_secondary', 'settlement_sources',
                          'settlement_source', 'price_source', 'underlying'):
                if series.get(field) is not None:
                    item[field] = series[field]
            item['metadata_fields'] = sorted(series.keys())
            item['exchange_index'] = series.get('exchange_index')
            item['product_metadata'] = series.get('product_metadata')
        except Exception as exc:
            item['error'] = type(exc).__name__ + ': ' + str(exc)
        output['series'].append(item)
    print(json.dumps(output, indent=2, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
