"""按查询块计算相似度，保持完整记忆、FP32 和原始公式。"""
def bounded_similarity(original, guard):
    def calculate(mk, ms, qk, qe, add_batch_dim=False, uncert_mask=None):
        import torch
        if add_batch_dim:
            mk, qk = mk.unsqueeze(0), qk.unsqueeze(0)
            ms = ms.unsqueeze(0) if ms is not None else None
            qe = qe.unsqueeze(0) if qe is not None else None
        qk = qk.flatten(2)
        qe = qe.flatten(2) if qe is not None else None
        uncertainty = uncert_mask.flatten(2) if uncert_mask is not None else None
        batch, _, queries = qk.shape
        keys = mk.flatten(2).shape[-1]
        block = min(256, queries)
        # 最终矩阵仍需完整保留，但临时矩阵仅保留一个查询块。
        guard(batch * keys * (queries + 4 * block) * qk.element_size() + 512 * 2**20)
        output = torch.empty((batch, keys, queries), dtype=qk.dtype, device=qk.device)
        for start in range(0, queries, block):
            end = start + block
            output[:, :, start:end] = original(mk, ms, qk[:, :, start:end],
                qe[:, :, start:end] if qe is not None else None,
                uncert_mask=uncertainty[:, :, start:end] if uncertainty is not None else None)
        return output
    return calculate
