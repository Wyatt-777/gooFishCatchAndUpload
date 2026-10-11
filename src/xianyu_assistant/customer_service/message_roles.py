"""One bounded DOM evidence reader and one role resolver for every adapter path."""
from __future__ import annotations

import hashlib
import json

from xianyu_assistant.customer_service.models import MessageDirection

# No waiting, scrolling, network access, or inference from message text. Legacy
# nesting selectors are retained as diagnostics, never as author identity.
ROLE_READER_JS = """function readRole(e, s) {
    const raw=e.getAttribute(s.direction_attribute);
    const incoming=Boolean(s.incoming_direction_selector && e.querySelector(s.incoming_direction_selector));
    const outgoing=Boolean(s.outgoing_direction_selector && e.querySelector(s.outgoing_direction_selector));
    const avatars=e.querySelectorAll('img.avatar--e05bt3Ju');
    const content=(s.text_selector && e.querySelector(s.text_selector)) || e.querySelector('.message-content--kBUbolyy');
    let geometry=null;
    if(avatars.length===1 && content) {
        const a=avatars[0].getBoundingClientRect(), c=content.getBoundingClientRect();
        const visible=a.width>0 && a.height>0 && c.width>0 && c.height>0;
        if(visible && a.right<=c.left+2) geometry='incoming';
        if(visible && a.left>=c.right-2) geometry='outgoing';
    }
    let sender=null, self=null, first=false;
    const fk=Object.keys(e).find(k=>k.startsWith('__reactFiber'));
    let fiber=fk ? e[fk] : null;
    for(let depth=0; fiber && depth<12; depth++,fiber=fiber.return) {
        const p=fiber.memoizedProps || {};
        const nested=p.message || p.messageInfo;
        if(nested && (nested.messageId!=null || nested.msgId!=null || nested.message_id!=null)) {
            for(const field of ['isSelf','isMine','fromSelf','isOwn','fromMe']) {
                if(self===null && typeof p[field]==='boolean') self=p[field];
            }
        }
        for(const v of [p.message,p.messageInfo,p.data,p]) {
            if(!v || typeof v!=='object') continue;
            if(v.messageId==null && v.msgId==null && v.message_id==null) continue;
            const id=v.senderId ?? v.senderUserId ?? v.fromUserId ?? v.sender?.userId;
            if(sender===null && id!=null && String(id)) sender=String(id);
            for(const field of ['isSelf','isMine','fromSelf','isOwn','fromMe']) {
                if(self===null && typeof v[field]==='boolean') self=v[field];
            }
            first=first || v.isFirstMessage===true || v.isFirstContact===true;
        }
    }
    return {raw_direction:raw, geometry, structure_incoming:incoming,
        structure_outgoing:outgoing, sender_id:sender, is_self:self, first_message:first};
}"""


def resolve_role(raw: dict[str, object], *, merchant_id: str | None = None,
                 buyer_id: str | None = None) -> tuple[MessageDirection, str, str]:
    """Resolve trusted evidence; contradictory strong signals remain unknown."""
    strong: list[tuple[str, str]] = []
    declared = str(raw.get("raw_direction") or "").casefold()
    if declared in {"incoming", "outgoing"}:
        strong.append((declared, "platform_attribute"))
    sender = str(raw.get("sender_id") or "")
    if sender and merchant_id and buyer_id and merchant_id != buyer_id:
        if sender == merchant_id:
            strong.append(("outgoing", "platform_sender_id"))
        elif sender == buyer_id:
            strong.append(("incoming", "platform_sender_id"))
    if isinstance(raw.get("is_self"), bool):
        strong.append(("outgoing" if raw["is_self"] else "incoming", "platform_self_flag"))
    geometry = raw.get("geometry")
    if geometry in {"incoming", "outgoing"}:
        strong.append((str(geometry), "geometry"))
    roles = {role for role, _ in strong}
    direction = MessageDirection(next(iter(roles))) if len(roles) == 1 else MessageDirection.UNKNOWN
    source = strong[0][1] if len(roles) == 1 else "role_conflict" if roles else "role_unresolved"
    evidence = {"raw_direction": declared or None, "geometry": geometry,
                "structure_incoming": bool(raw.get("structure_incoming")),
                "structure_outgoing": bool(raw.get("structure_outgoing")),
                "sender_ref": hashlib.sha256(sender.encode()).hexdigest()[:12] if sender else None,
                "self_flag": raw.get("is_self"), "signals": strong,
                "resolution": source}
    return direction, source, json.dumps(evidence, ensure_ascii=False)


def role_is_trusted(direction: MessageDirection, source: str) -> bool:
    return direction is not MessageDirection.UNKNOWN and source in {
        "platform_attribute", "platform_sender_id", "platform_self_flag",
        "geometry", "verified_send_id", "adapter",
    }
