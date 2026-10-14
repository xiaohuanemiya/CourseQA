"""Read-only verification of all original files after the authorized knowledge reset."""
import hashlib
import json
from pathlib import Path

import httpx
from dotenv import dotenv_values
from pypdf import PdfReader


def main():
    originals={p.name:p for p in Path('AdvancedMathematicsData(1)').iterdir() if p.suffix.lower() in {'.pdf','.docx'}}
    with httpx.Client(base_url='http://127.0.0.1:8820/api',timeout=180,
                      headers={'X-Requested-With':'calculus-studio'}) as client:
        response=client.post('/auth/login',json={'username':'admin','password':dotenv_values('.env')['ADMIN_PASSWORD']})
        response.raise_for_status()
        knowledge_bases=client.get('/admin/knowledge-bases').json()
        summary={'expected_documents':len(originals),'pdf_pages':0,'documents':[],'knowledge_bases':[],
            'all_pending_review':True,'published_vectors':0,'semantic_relations':0}
        seen=[]
        for kb in knowledge_bases:
            response=client.get(f"/admin/knowledge-bases/{kb['id']}/contents")
            response.raise_for_status()
            data=response.json()
            assert not data['relations'] and not data['questions'] and not data['releases']
            assert not kb['current_release_id']
            assert all(not n['reviewed'] and not n['withdrawn'] for n in data['knowledge'])
            if data['documents']:
                summary['knowledge_bases'].append({'name':kb['name'],'documents':len(data['documents']),
                    'knowledge':len(data['knowledge']),'formulas':sum(n['kind']=='formula' for n in data['knowledge'])})
            for document in data['documents']:
                name=document['filename']
                assert name in originals and document['state']=='review' and not document['withdrawn'],(name,document['state'])
                assert hashlib.sha256(originals[name].read_bytes()).hexdigest()==document['checksum']
                seen.append(name)
                item={'filename':name,'document_id':document['id'],'pages':None}
                if name.lower().endswith('.pdf'):
                    expected=len(PdfReader(originals[name]).pages)
                    response=client.get(f"/admin/documents/{document['id']}/recognition")
                    response.raise_for_status()
                    report=response.json()
                    assert [p['page'] for p in report['pages']]==list(range(1,expected+1)),name
                    assert len({b['locator'] for b in report['blocks']})==len(report['blocks']),name
                    assert all(b['asset_id'] in report['assets'] for b in report['blocks']),name
                    item.update(pages=expected,counts=report['counts'],parser=report['parser'])
                    summary['pdf_pages']+=expected
                summary['documents'].append(item)
        assert sorted(seen)==sorted(originals),'Missing or duplicated original documents'
        summary['knowledge']=sum(k['knowledge'] for k in summary['knowledge_bases'])
        summary['formulas']=sum(k['formulas'] for k in summary['knowledge_bases'])
        summary['status']='verified'
        path=Path('artifacts/reimport-summary.json')
        path.write_text(json.dumps(summary,ensure_ascii=False,indent=2))
        print(json.dumps({k:summary[k] for k in ['status','expected_documents','pdf_pages','knowledge','formulas']},ensure_ascii=False))


if __name__=='__main__':
    main()
