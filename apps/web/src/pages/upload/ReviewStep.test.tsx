import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import ReviewStep from './ReviewStep'

it('shows every applicable quality failure and blocks publish', () => {
  render(<ReviewStep
    run={{
      id: 'run-1',
      original_filename: 'orders.csv',
      source_definition_id: 'source-1',
      status: 'quality_failed',
      summary: { row_count: 1 },
      quality_result: {
        checks: [
          { key: 'row_count', applicable: true, status: 'passed', actual: 1 },
          { key: 'expected_volume', applicable: true, status: 'failed', actual: 1, minimum: 4, maximum: 7 },
          { key: 'semantic_model', applicable: true, status: 'passed' },
        ],
      },
    }}
    duplicate={false}
    busy={false}
    note=""
    onNote={vi.fn()}
    onPublish={vi.fn()}
  />)
  expect(screen.getByText('文件记录数量符合预期')).toBeInTheDocument()
  expect(screen.getByText('实际 1 条，预期 4 至 7 条')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '确认并更新看板' })).toBeDisabled()
})

it('uses business wording for multi-store reconciliation and requires an admin correction reason', async () => {
  const onCorrect = vi.fn()
  const onReason = vi.fn()
  render(<ReviewStep
    run={{
      id: 'run-2',
      original_filename: 'orders-revised.csv',
      source_definition_id: 'source-1',
      status: 'quality_passed',
      summary: { store_ids: ['store-a', 'store-b'] },
      quality_result: { checks: [{ key: 'cross_source:0:required_source', status: 'passed' }] },
    }}
    duplicate={false}
    busy={false}
    note=""
    onNote={vi.fn()}
    onPublish={vi.fn()}
    correctionRequired
    canCorrect
    correctionReason="月结后收到平台修订文件"
    onCorrectionReason={onReason}
    onCorrect={onCorrect}
  />)
  expect(screen.getByText('2 个店铺')).toBeInTheDocument()
  expect(screen.getByText('本月必需文件已齐全并核对')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: '管理员确认更正并更新' }))
  expect(onCorrect).toHaveBeenCalledOnce()
})
