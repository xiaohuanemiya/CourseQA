from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .db import session
from .security import admin
from .services import graph_explorer as graph

router=APIRouter(prefix="/api/admin/graph",dependencies=[Depends(admin)],tags=["Admin graph explorer"])


@router.get("/overview")
def overview(db:Session=Depends(session)):
    return graph.overview(db)


@router.get("")
def query(kb_id:str|None=None,version:Literal["draft","published"]="draft",include_sources:bool=True,
          q:str=Query("",max_length=200),kind:str="",
          focus:str|None=None,hops:int=Query(1,ge=1,le=2),limit:int=Query(200,ge=1,le=500),
          offset:int=Query(0,ge=0),db:Session=Depends(session)):
    return graph.query_graph(db,kb_id,version,include_sources,q,kind,focus,hops,limit,offset)


@router.get("/nodes/{node_id}")
def node(node_id:str,kb_id:str|None=None,version:Literal["draft","published"]="draft",db:Session=Depends(session)):
    return graph.node_detail(db,node_id,kb_id,version)


@router.get("/path")
def path(source:str,target:str,kb_id:str|None=None,version:Literal["draft","published"]="draft",
         include_sources:bool=False,directed:bool=False,db:Session=Depends(session)):
    return graph.find_path(db,source,target,kb_id,version,include_sources,directed)
