import { render, screen } from '@testing-library/react'
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
