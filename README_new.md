# Sanchar Mitra — Debit Service

This is a standalone backend service that processes wallet adjustments (debits) for Sanchar Mitra services via the Pyro API. It supports **FancySale**,  **SimSwap** and **ESIM**.

## Architecture & Data Flow

The service operates primarily via a background scheduler that executes the following loop:
1. **Fetch**: 
   - **FANCYSALE**  Reads eligible records from the Oracle database `CAF_ADMIN.VANITYSALE_FRANCH_DATA`  where `CAF_ENTRY_DONE IN ('N', 'QM', 'QB')`.
   - **SIMSWAP**  Reads eligible records from the Oracle database  `CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS` where `CAF_ENTRY_DONE IN ('N', 'QM', 'QB')`.
   - **ESIM**  Reads eligible records from the Oracle database  `CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS` where `CAF_ENTRY_DONE IN ('N', 'QM', 'QB')`.
2. **Claim**: Marks the rows as `P` (Processing) atomically to prevent duplicate processing.
3. **Submit**: Calls the Pyro API (`/erp-stock-api/service-wallet-adjustment`) with the decrypted MPIN and transaction details.
4. **Writeback**:
   - **FANCYSALE** Updates the Oracle database(VANITYSALE_FRANCH_DATA) with the result (`Y` for Success, `R` for Rejected).For FANCYSALE, Praveen Sir will update the BCD table through his existing scheduled process.
   - **SIMSWAP** Updates the Oracle database(SIMSWAP_AMOUNT_DEDUCT_REQUESTS) with the result (`Y` for Success, `R` for Rejected).Also  UPDATE CAF_ADMIN.BCD
            SET    ACTIVATION_STATUS = 'AI'
            WHERE  GSMNUMBER         = :gsmnumber
              AND  ACTIVATION_STATUS = 'IF'
   - **ESIM** Updates the Oracle database(SIMSWAP_AMOUNT_DEDUCT_REQUESTS) with the result (`Y` for Success, `R` for Rejected). Also UPDATE CAF_ADMIN.SIM_SWAP_DATA
            SET    ACTIVATION_STATUS = 'AI'
            WHERE  GSMNUMBER         = :gsmnumber
              AND  ACTIVATION_STATUS = 'IF'
5. **Audit Log**: Records the full transaction lifecycle to a PostgreSQL database (`debit_txn_log`) for monitoring and auditing. service_type will identify whether FANCYSALE, SIMSWAP or ESIM logs.
